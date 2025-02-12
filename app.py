from flask import Flask, request, jsonify, render_template
from pymongo import MongoClient
import os
from flask_cors import CORS
from math import radians, sin, cos, sqrt, asin
from clarifai_grpc.grpc.api import resources_pb2, service_pb2, service_pb2_grpc
from clarifai_grpc.channel.clarifai_channel import ClarifaiChannel

import json

# Load food_cuisines from JSON file
with open('food_cuisines.json') as f:
    food_cuisines = json.load(f)

app = Flask(__name__)
CORS(app)

# Securely load MongoDB URI
MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)

db = client["test"]  # Replace with your database name
collection = db["restaurants"]  # Replace with your collection name

# Clarifai gRPC Setup
CLARIFAI_API_KEY = os.getenv("CLARIFAI_API_KEY")
channel = ClarifaiChannel.get_grpc_channel()
stub = service_pb2_grpc.V2Stub(channel)
metadata = (("authorization", f"Key {CLARIFAI_API_KEY}"),)

@app.route('/')
def index():
    return render_template('restaurant_list.html')

@app.route('/restaurants', methods=['GET'])
def get_restaurants():
    page = request.args.get('page', 1, type=int)
    size = request.args.get('size', 10, type=int)
    start = (page - 1) * size

    # Fetch restaurants from MongoDB
    cursor = collection.find({}, {
        "_id": 0,
        "restaurant.name": 1,
        "restaurant.cuisines": 1,
        "restaurant.photos_url": 1,
        "restaurant.featured_image": 1,  # Fetching the featured image
        "restaurant.url": 1
    }).skip(start).limit(size)

    restaurants = list(cursor)

    # Ensure every restaurant has an image
    for r in restaurants:
        if "restaurant" in r and "featured_image" in r["restaurant"] and r["restaurant"]["featured_image"]:
            r["image"] = r["restaurant"]["featured_image"]
        else:
            r["image"] = "https://via.placeholder.com/100"  # Default image

    response = {
        "page": page,
        "size": size,
        "total_restaurants": collection.count_documents({}),
        "restaurants": restaurants
    }

    return jsonify(response)
# Function to calculate Haversine distance (distance between two lat/lng points)
def haversine(lat1, lon1, lat2, lon2):
    R = 6371  # Earth radius in km
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    
    return R * c  # Returns distance in km

@app.route('/restaurants/nearby', methods=['GET'])
def get_nearby_restaurants():
    try:
        lat = float(request.args.get('lat'))
        lng = float(request.args.get('lng'))
        radius_km = float(request.args.get('radius', 5))  # Default radius = 5 km
        page = int(request.args.get('page', 1))
        size = int(request.args.get('size', 10))
        start = (page - 1) * size

        # Fetch all restaurants (filtering is done later)
        cursor = collection.find({}, {
            "_id": 0,
            "restaurant.name": 1,
            "restaurant.cuisines": 1,
            "restaurant.photos_url": 1,
            "restaurant.featured_image": 1,
            "restaurant.url": 1,
            "restaurant.location.latitude": 1,
            "restaurant.location.longitude": 1
        })

        nearby_restaurants = []

        for r in cursor:
            if "restaurant" in r and "location" in r["restaurant"]:
                rest_lat = float(r["restaurant"]["location"].get("latitude", 0))
                rest_lng = float(r["restaurant"]["location"].get("longitude", 0))

                # Compute distance
                distance = haversine(lat, lng, rest_lat, rest_lng)

                if distance <= radius_km:
                    r["distance_km"] = round(distance, 2)  # Add distance info
                    r["image"] = r["restaurant"].get("featured_image", "https://via.placeholder.com/100")  # Default image
                    nearby_restaurants.append(r)

        total_restaurants = len(nearby_restaurants)
        paginated_restaurants = nearby_restaurants[start:start + size]

        response = {
            "latitude": lat,
            "longitude": lng,
            "radius_km": radius_km,
            "page": page,
            "size": size,
            "total_restaurants": total_restaurants,
            "restaurants": paginated_restaurants
        }

        return jsonify(response)

    except Exception as e:
        print(f"Error: {str(e)}")  # Log the error for debugging
        return jsonify({"error": str(e)}), 500
    

#@app.route('/test', methods=['POST'])
#def test_api():
#    return jsonify({"message": "POST request is working!"})
@app.route('/restaurants/by-image', methods=['POST'])
def get_restaurants_by_image():
    try:
        if 'image' not in request.files:
            return jsonify({"error": "No image provided"}), 400
        
        image_file = request.files['image']
        image_bytes = image_file.read()

        # Send image to Clarifai's Food Model using gRPC
        request_data = service_pb2.PostModelOutputsRequest(
            model_id="food-item-recognition",
            inputs=[resources_pb2.Input(data=resources_pb2.Data(image=resources_pb2.Image(base64=image_bytes)))],
        )
        response = stub.PostModelOutputs(request_data, metadata=metadata)

        if response.status.code != 10000:  # Check if API call was successful
            return jsonify({"error": "Clarifai API error"}), 500

        # Extract cuisine-related keywords from Clarifai predictions
        detected_foods = [concept.name.lower() for concept in response.outputs[0].data.concepts]

        if not detected_foods:
            return jsonify({"error": "No cuisine detected in the image"}), 400

        # Classify detected foods into cuisines using food_cuisines
        detected_cuisines = set()
        for food in detected_foods:
            for cuisine, foods in food_cuisines.items():
                if food in foods:
                    detected_cuisines.add(cuisine)
        
        if not detected_cuisines:
            return jsonify({"error": "No known cuisines detected for the food items"}), 400

        # Query MongoDB dynamically based on detected cuisines
        query = {
            "restaurant.cuisines": {
                "$regex": "|".join([cuisine.capitalize() for cuisine in detected_cuisines]),  # Using regex to match any of the detected cuisines
                "$options": "i"  # Case-insensitive search
            }
        }

        cursor = collection.find(query, {
            "_id": 0,
            "restaurant.name": 1,
            "restaurant.cuisines": 1,
            "restaurant.photos_url": 1,
            "restaurant.featured_image": 1,
            "restaurant.url": 1
        }).limit(10)  # Limit to top 7 results

        restaurants = list(cursor)

        # Add images to response
        for r in restaurants:
            r["image"] = r["restaurant"].get("featured_image", "https://via.placeholder.com/100")

        response_data = {
            "detected_cuisines": list(detected_cuisines),
            "total_restaurants": len(restaurants),
            "restaurants": restaurants
        }

        return jsonify(response_data)

    except Exception as e:
        print(f"Error: {str(e)}")  # Log the error for debugging
        return jsonify({"error": str(e)}), 500

@app.route('/restaurants/search', methods=['POST'])
def search_restaurants():
    try:
        if 'image' not in request.files:
            return jsonify({"error": "No image provided"}), 400
        
        image_file = request.files['image']  # Image file
        lat = float(request.form['latitude'])  # Latitude from the frontend
        lon = float(request.form['longitude'])  # Longitude from the frontend
        radius = float(request.form.get('radius', 5))  # Default 5 km radius

        # Send image to Clarifai's Food Model using gRPC
        image_bytes = image_file.read()
        request_data = service_pb2.PostModelOutputsRequest(
            model_id="food-item-recognition",
            inputs=[resources_pb2.Input(data=resources_pb2.Data(image=resources_pb2.Image(base64=image_bytes)))],
        )
        response = stub.PostModelOutputs(request_data, metadata=metadata)

        if response.status.code != 10000:  # Check if API call was successful
            return jsonify({"error": "Clarifai API error"}), 500

        # Extract cuisine-related keywords from Clarifai predictions
        detected_foods = [concept.name.lower() for concept in response.outputs[0].data.concepts]

        if not detected_foods:
            return jsonify({"error": "No cuisine detected in the image"}), 400

        # Classify detected foods into cuisines using food_cuisines
        detected_cuisines = set()
        for food in detected_foods:
            for cuisine, foods in food_cuisines.items():
                if food in foods:
                    detected_cuisines.add(cuisine)

        if not detected_cuisines:
            return jsonify({"error": "No known cuisines detected for the food items"}), 400

        # Query MongoDB dynamically based on detected cuisines and location
        query = {
            "restaurant.cuisines": {
                "$regex": "|".join([cuisine.capitalize() for cuisine in detected_cuisines]),  # Using regex to match any of the detected cuisines
                "$options": "i"  # Case-insensitive search
            },
            "restaurant.location.latitude": {"$exists": True},
            "restaurant.location.longitude": {"$exists": True}
        }

        cursor = collection.find(query, {
            "_id": 0,
            "restaurant.name": 1,
            "restaurant.cuisines": 1,
            "restaurant.photos_url": 1,
            "restaurant.featured_image": 1,
            "restaurant.url": 1,
            "restaurant.location.latitude": 1,
            "restaurant.location.longitude": 1
        })

        nearby_restaurants = []

        for r in cursor:
            if "restaurant" in r and "location" in r["restaurant"]:
                rest_lat = float(r["restaurant"]["location"].get("latitude", 0))
                rest_lng = float(r["restaurant"]["location"].get("longitude", 0))

                # Compute distance
                distance = haversine(lat, lon, rest_lat, rest_lng)

                if distance <= radius:
                    r["distance_km"] = round(distance, 2)  # Add distance info
                    r["image"] = r["restaurant"].get("featured_image", "https://via.placeholder.com/100")  # Default image
                    nearby_restaurants.append(r)

        total_restaurants = len(nearby_restaurants)
        paginated_restaurants = nearby_restaurants[:10]  # Adjust pagination if needed

        response_data = {
            "detected_cuisines": list(detected_cuisines),
            "total_restaurants": total_restaurants,
            "restaurants": paginated_restaurants
        }

        return jsonify(response_data)

    except Exception as e:
        print(f"Error: {str(e)}")  # Log the error for debugging
        return jsonify({"error": str(e)}), 500



if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
