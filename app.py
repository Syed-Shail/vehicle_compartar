from flask import Flask, render_template, request, redirect, session, url_for
import json
import os
import re

# --- Firebase imports and initialization ---
import firebase_admin
from firebase_admin import credentials, db, auth

def get_firebase_config():
    # Reads firebase_keys.txt and returns a dict with keys
    with open('firebase_keys.txt', 'r') as f:
        keys = {}
        for line in f:
            if ':' in line:
                k, v = line.strip().split(':', 1)
                keys[k.strip()] = v.strip()
    return keys

firebase_keys = get_firebase_config()
if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_keys['service_account_path'])
    firebase_admin.initialize_app(cred, {
        'databaseURL': firebase_keys['database_url']
    })
FIREBASE_API_KEY = firebase_keys.get('api_key', '')

app = Flask(__name__)
app.secret_key = 'your_secret_key_here'

# --- Firebase DB paths ---
USERS_PATH = '/users'
CARS_PATH = '/cars'

# Load users from Firebase
def load_users():
    users_ref = db.reference(USERS_PATH)
    users = users_ref.get() or {}
    return {k: v['password'] for k, v in users.items()}

# Save user to Firebase
def save_user(username, password):
    users_ref = db.reference(USERS_PATH)
    users_ref.child(username).set({'password': password})

# Load cars from Firebase
def load_cars():
    cars_ref = db.reference(CARS_PATH)
    cars = cars_ref.get()
    if not cars:
        return []
    if isinstance(cars, dict):
        return [v for v in cars.values()]
    elif isinstance(cars, list):
        return [v for v in cars if v]
    else:
        return []

# Save cars to Firebase (overwrite all)
def save_cars(cars):
    cars_ref = db.reference(CARS_PATH)
    cars_dict = {str(car['id']): car for car in cars}
    cars_ref.set(cars_dict)

# AI scoring function
def score_car(car, budget, fuel_type, seats, car_type):
    score = 0
    if car['price'] <= budget:
        score += 2
    if fuel_type and car['fuel_type'] == fuel_type:
        score += 2
    if car['seats'] >= seats:
        score += 1
    if car_type and car['type'] == car_type:
        score += 1
    return score

def get_user_role(uid):
    users_ref = db.reference(USERS_PATH)
    user = users_ref.child(uid).get()
    print("User DB entry for UID", uid, ":", user)
    return user.get('role', 'user') if user else 'user'

def verify_firebase_token(id_token):
    try:
        decoded_token = auth.verify_id_token(id_token)
        return decoded_token
    except Exception:
        return None

@app.route('/')
def home():
    cars = load_cars()
    username = session.get('username', 'Guest')
    return render_template('home.html', username=username, cars=cars)

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        import requests
        api_key = FIREBASE_API_KEY
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}"
        payload = {
            "email": email,
            "password": password,
            "returnSecureToken": True
        }
        resp = requests.post(url, json=payload)
        print("Firebase login response:", resp.text)
        if resp.status_code == 200:
            data = resp.json()
            id_token = data['idToken']
            decoded = verify_firebase_token(id_token)
            if decoded:
                session['username'] = email
                session['uid'] = decoded['uid']
                session['id_token'] = id_token
                session['role'] = get_user_role(decoded['uid'])
                print("Logged in as:", email, "Role:", session['role'])
                if session['role'] != 'admin':
                    print("WARNING: You are logged in but not an admin. Add/edit car options will be hidden.")
                return redirect(url_for('home'))
        else:
            try:
                error = resp.json().get('error', {}).get('message', 'Invalid credentials')
            except Exception:
                error = 'Invalid credentials'
            print("Login error:", error)
            return render_template('login.html', error=error)
    return render_template('login.html', error=error)

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/add_car', methods=['GET', 'POST'])
@login_required
def add_car():
    if request.method == 'POST':
        cars = load_cars()
        new_car = {
            "id": len(cars) + 1,
            "name": request.form['name'],
            "price": int(request.form['price']),
            "fuel_type": request.form['fuel_type'],
            "seats": int(request.form['seats']),
            "economy": float(request.form['economy']),
            "type": request.form['type'],
            "brand": request.form.get('brand', '')
        }
        cars.append(new_car)
        save_cars(cars)
        return redirect(url_for('home'))

    return render_template('add_car.html')

@app.route('/edit_car/<int:car_id>', methods=['GET', 'POST'])
@login_required
def edit_car(car_id):
    cars = load_cars()
    car = next((c for c in cars if c["id"] == car_id), None)

    if not car:
        return "Car not found", 404

    if request.method == 'POST':
        car['name'] = request.form['name']
        car['price'] = int(request.form['price'])
        car['fuel_type'] = request.form['fuel_type']
        car['seats'] = int(request.form['seats'])
        car['economy'] = float(request.form['economy'])
        car['type'] = request.form['type']
        car['brand'] = request.form.get('brand', '')
        save_cars(cars)
        return redirect(url_for('home'))

    return render_template('edit_car.html', car=car)

from flask import Markup, session

# Helper to format numbers in Indian style (e.g., 45,00,000)
def format_inr(number):
    s = str(int(number))
    if len(s) <= 3:
        return s
    else:
        last3 = s[-3:]
        rest = s[:-3]
        parts = []
        while len(rest) > 2:
            parts.append(rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.append(rest)
        return ','.join(reversed(parts)) + ',' + last3

@app.route('/chat', methods=['GET', 'POST'])
def chat():
    response = None
    if request.method == 'POST':
        message = request.form.get('message', '').strip().lower()
        cars = load_cars()

        # Helper: parse budget from message
        def extract_budget(msg):
            match = re.search(r'(?:under|below|less than)\s*([\d,\.]+)\s*(lakh|lakhs|k|crore|cr)?', msg)
            if match:
                num = float(match.group(1).replace(',', ''))
                unit = match.group(2)
                if unit in ['lakh', 'lakhs']:
                    return int(num * 100000)
                elif unit in ['crore', 'cr']:
                    return int(num * 10000000)
                elif unit == 'k':
                    return int(num * 1000)
                else:
                    return int(num)
            match = re.search(r'(\d{6,})', msg)
            if match:
                return int(match.group(1))
            return None

        # Helper: parse seats
        def extract_seats(msg):
            match = re.search(r'(\d+)\s*(seater|seats?)', msg)
            if match:
                return int(match.group(1))
            return None

        # Helper: parse brand
        def extract_brand(msg, cars):
            brands = set(car['brand'].lower() for car in cars if car.get('brand'))
            for brand in brands:
                if brand in msg:
                    return brand
            return None

        # Helper: parse fuel type
        def extract_fuel(msg):
            for fuel in ['petrol', 'diesel', 'electric', 'cng']:
                if fuel in msg:
                    return fuel.capitalize()
            return None

        # Helper: parse car type
        def extract_type(msg):
            for t in ['suv', 'sedan', 'hatchback', 'mpv']:
                if t in msg:
                    return t.capitalize()
            return None

        # --- Main logic ---
        trigger_keywords = ["suggest", "recommend", "find", "show", "best", "good", "options"]
        has_trigger = any(word in message for word in trigger_keywords)
        has_type = extract_type(message) is not None
        has_budget = extract_budget(message) is not None

        if "compare" in message and " and " in message:
            names = message.replace("compare", "").split(" and ")
            if len(names) == 2:
                car1 = next((c for c in cars if names[0].strip() in c['name'].lower()), None)
                car2 = next((c for c in cars if names[1].strip() in c['name'].lower()), None)
                if car1 and car2:
                    response = (
                        f"<b>{car1['name']}</b> (₹{format_inr(car1['price'])}, {car1['fuel_type']}, {car1['seats']} seats, {car1['economy']} km/l, {car1['type']})"
                        f"<br><b>VS</b><br>"
                        f"<b>{car2['name']}</b> (₹{format_inr(car2['price'])}, {car2['fuel_type']}, {car2['seats']} seats, {car2['economy']} km/l, {car2['type']})"
                    )
                else:
                    response = "One or both cars not found."
            else:
                response = "Please specify two cars to compare (e.g. 'compare Honda City and Tata Nexon EV')."
        elif has_trigger or has_type or has_budget:
            budget = extract_budget(message)
            fuel = extract_fuel(message)
            car_type = extract_type(message)
            seats = extract_seats(message)
            brand = extract_brand(message, cars)

            filtered = cars
            if budget:
                filtered = [c for c in filtered if c['price'] <= budget]
            if fuel:
                filtered = [c for c in filtered if c['fuel_type'].lower() == fuel.lower()]
            if car_type:
                filtered = [c for c in filtered if c['type'].lower() == car_type.lower()]
            if seats:
                filtered = [c for c in filtered if c['seats'] >= seats]
            if brand:
                filtered = [c for c in filtered if brand in c['brand'].lower()]

            if filtered:
                filtered = sorted(filtered, key=lambda c: c['price'])
                top = filtered[:5]
                response = "Here are some matching cars:<br>" + "<br>".join(
                    f"{car['name']} (₹{format_inr(car['price'])}, {car['fuel_type']}, {car['seats']} seats, {car['type']})"
                    for car in top
                )
            else:
                response = "Sorry, I couldn't find any cars matching your criteria."
        elif "electric" in message and "suv" in message:
            matches = [car for car in cars if car['fuel_type'].lower() == 'electric' and car['type'].lower() == 'suv']
            if matches:
                response = "Electric SUVs:<br>" + "<br>".join(f"{car['name']} (₹{format_inr(car['price'])})" for car in matches)
            else:
                response = "No electric SUVs found."
        elif "hello" in message or "hi" in message:
            response = "Hello! 👋 How can I help you with car recommendations or comparisons today?"
        else:
            response = (
                "I'm your car assistant! 🚗<br>"
                "You can ask me things like:<br>"
                "- Suggest an electric SUV under 15 lakhs<br>"
                "- Recommend a 7 seater diesel car<br>"
                "- Compare Honda City and Tata Nexon EV<br>"
                "- Show me Hyundai hatchbacks below 10 lakh"
            )
        response = Markup(response)
    return render_template('chatbot.html', response=response)

@app.route('/smart_compare', methods=['GET', 'POST'])
def smart_compare():
    result = None
    if request.method == 'POST':
        car1_name = request.form.get('car1', '').strip().lower()
        car2_name = request.form.get('car2', '').strip().lower()
        cars = load_cars()
        car1 = next((c for c in cars if car1_name in c['name'].lower()), None)
        car2 = next((c for c in cars if car2_name in c['name'].lower()), None)
        if car1 and car2:
            result = (
                f"<b>{car1['name']}</b> (₹{format_inr(car1['price'])}, {car1['fuel_type']}, {car1['seats']} seats, {car1['economy']} km/l, {car1['type']})"
                f"<br><b>VS</b><br>"
                f"<b>{car2['name']}</b> (₹{format_inr(car2['price'])}, {car2['fuel_type']}, {car2['seats']} seats, {car2['economy']} km/l, {car2['type']})"
            )
        else:
            result = "One or both cars not found. Please check the names."
        result = Markup(result)
    return render_template('smart_compare.html', result=result)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        if not email or not password:
            return 'Email and password are required.'
        if len(password) < 6:
            return 'Password must be at least 6 characters.'
        try:
            try:
                auth.get_user_by_email(email)
                return 'Email already registered.'
            except auth.UserNotFoundError:
                pass

            user_record = auth.create_user(email=email, password=password)
            uid = user_record.uid
            users_ref = db.reference(USERS_PATH)
            users_ref.child(uid).set({'role': 'user'})
            return redirect(url_for('login'))
        except Exception as e:
            return f'Error creating user: {e}'
    return render_template('register.html')

@app.route('/import_cars')
def import_cars():
    # Try both possible JSON file locations (project root and Downloads)
    json_paths = [
        os.path.join(os.getcwd(), 'vehicles_sample.json'),
        os.path.join(os.getcwd(), 'updated_vehicles.json'),
        os.path.expanduser('~/Downloads/updated_vehicles.json')
    ]
    cars_from_file = None
    for path in json_paths:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                cars_from_file = json.load(f)
            break
    if cars_from_file is None:
        return "No vehicles_sample.json or updated_vehicles.json file found in the project directory or Downloads folder."

    existing_cars = load_cars()
    existing_ids = {car['id'] for car in existing_cars}
    existing_names = {car['name'].lower() for car in existing_cars}
    new_cars = []
    for car in cars_from_file:
        if car['id'] not in existing_ids and car['name'].lower() not in existing_names:
            new_cars.append(car)
    if new_cars:
        all_cars = existing_cars + new_cars
        save_cars(all_cars)
        return f"Imported {len(new_cars)} new cars."
    else:
        return "No new cars to import."

@app.route('/ai_quiz', methods=['GET', 'POST'])
def ai_quiz():
    questions = [
        {
            "key": "purpose",
            "question": "What will you use the car for most? (Daily commute, Family trips, Long highway drives, City errands, Business/Status, Adventure/Off-road)",
            "type": "select",
            "options": [
                "Daily commute",
                "Family trips",
                "Long highway drives",
                "City errands",
                "Business/Status",
                "Adventure/Off-road"
            ]
        },
        {
            "key": "income",
            "question": "What is your approximate annual income (in lakhs)?",
            "type": "number"
        },
        {
            "key": "eco_pref",
            "question": "Do you prefer fuel savings, eco-friendliness, or performance?",
            "type": "select",
            "options": [
                "Fuel savings",
                "Eco-friendliness",
                "Performance",
                "No preference"
            ]
        },
        {
            "key": "family_size",
            "question": "How many people (including you) will usually travel in the car?",
            "type": "number"
        },
        {
            "key": "brand",
            "question": "Any brand you like? (optional)",
            "type": "text"
        }
    ]
    if 'quiz_answers' not in session:
        session['quiz_answers'] = {}
        session['quiz_step'] = 0

    quiz_answers = session['quiz_answers']
    quiz_step = session['quiz_step']

    if request.method == 'POST':
        prev_key = questions[quiz_step]['key']
        value = request.form.get(prev_key, '').strip()
        if questions[quiz_step]['type'] == 'number':
            try:
                value = int(value)
            except Exception:
                value = None
        quiz_answers[prev_key] = value
        session['quiz_answers'] = quiz_answers
        quiz_step += 1
        session['quiz_step'] = quiz_step

    if quiz_step < len(questions):
        q = questions[quiz_step]
        return render_template('ai_quiz.html', question=q, step=quiz_step+1, total=len(questions), answers=quiz_answers)
    else:
        # Map answers to car attributes
        purpose = quiz_answers.get('purpose', '')
        income = quiz_answers.get('income')
        eco_pref = quiz_answers.get('eco_pref', '')
        family_size = quiz_answers.get('family_size')
        brand = quiz_answers.get('brand', '').lower()

        # Budget estimation based on income (very rough, 40% of annual income)
        budget = None
        if income:
            budget = int(income) * 100000  # 1 lakh = 100000
            budget = int(budget * 0.4)

        # Purpose mapping
        car_type = None
        seats = None
        if purpose == "Family trips":
            car_type = None  # MPV/SUV preferred, but allow all
            seats = 6
        elif purpose == "Long highway drives":
            car_type = "Sedan"
        elif purpose == "City errands":
            car_type = "Hatchback"
        elif purpose == "Business/Status":
            car_type = None  # Prefer higher price, luxury brands
        elif purpose == "Adventure/Off-road":
            car_type = "SUV"
        elif purpose == "Daily commute":
            car_type = None  # Any, but prefer fuel savings

        # Eco/fuel preference mapping
        fuel_type = None
        if eco_pref == "Fuel savings":
            fuel_type = None  # Prefer high economy
        elif eco_pref == "Eco-friendliness":
            fuel_type = "Electric"
        elif eco_pref == "Performance":
            fuel_type = None  # Prefer higher price, higher power

        cars = load_cars()
        filtered = cars

        if budget:
            filtered = [c for c in filtered if c['price'] <= budget]
        if fuel_type:
            filtered = [c for c in filtered if c['fuel_type'].lower() == fuel_type.lower()]
        if car_type:
            filtered = [c for c in filtered if c['type'].lower() == car_type.lower()]
        if family_size:
            filtered = [c for c in filtered if c['seats'] >= int(family_size)]
        if brand:
            filtered = [c for c in filtered if brand in c['brand'].lower()]

        # If fuel savings, sort by economy descending
        if eco_pref == "Fuel savings":
            filtered = sorted(filtered, key=lambda c: -float(c.get('economy', 0)))
        # If performance, sort by price descending
        elif eco_pref == "Performance":
            filtered = sorted(filtered, key=lambda c: -c['price'])
        # If business/status, sort by price descending
        elif purpose == "Business/Status":
            filtered = sorted(filtered, key=lambda c: -c['price'])
        else:
            filtered = sorted(filtered, key=lambda c: c['price'])

        top = filtered[:5]
        session.pop('quiz_answers', None)
        session.pop('quiz_step', None)
        return render_template('ai_quiz.html', results=top, format_inr=format_inr, answers=quiz_answers)

@app.route('/multi_compare', methods=['GET', 'POST'])
def multi_compare():
    cars = load_cars()
    selected_cars = []
    alternatives = []
    error = None

    if request.method == 'POST':
        names = [
            request.form.get('car_name1', '').strip().lower(),
            request.form.get('car_name2', '').strip().lower(),
            request.form.get('car_name3', '').strip().lower(),
            request.form.get('car_name4', '').strip().lower()
        ]
        names = [n for n in names if n]
        if len(names) < 2:
            error = "Please enter at least 2 car names to compare."
        elif len(names) > 4:
            error = "You can compare up to 4 cars at a time."
        else:
            selected_cars = []
            for n in names:
                car = next((c for c in cars if n in c['name'].lower()), None)
                if car:
                    selected_cars.append(car)
            if len(selected_cars) < len(names):
                error = "One or more car names not found. Please check your input."
            else:
                # Suggest alternatives: cars of same type/price range not in selected
                types = set(car['type'] for car in selected_cars)
                min_price = min(car['price'] for car in selected_cars)
                max_price = max(car['price'] for car in selected_cars)
                price_margin = int((max_price - min_price) * 0.5) or 200000
                alt_candidates = [
                    car for car in cars
                    if car not in selected_cars and
                       (car['type'] in types or abs(car['price'] - min_price) <= price_margin)
                ]
                alternatives = sorted(alt_candidates, key=lambda c: abs(c['price'] - min_price))[:3]

    return render_template(
        'multi_compare.html',
        cars=cars,
        selected_cars=selected_cars,
        alternatives=alternatives,
        format_inr=format_inr,
        error=error
    )

@app.route('/compare', methods=['GET', 'POST'])
def compare():
    # Make /compare behave exactly like /multi_compare
    return multi_compare()

@app.route('/recommend', methods=['GET', 'POST'])
def recommend():
    cars = load_cars()
    top_matches = []
    categories = {}
    alternatives = []

    if request.method == 'POST':
        budget = request.form.get('budget', type=int)
        fuel_type = request.form.get('fuel_type', '')
        seats = request.form.get('seats', type=int)
        car_type = request.form.get('car_type', '')
        brand = request.form.get('brand', '').lower()

        # Validation: budget must not be negative or zero
        if budget is not None and budget < 0:
            budget = 0
        if seats is not None and seats < 2:
            seats = 2

        filtered = cars
        # Only apply budget filter if budget is positive
        if budget and budget > 0:
            filtered = [c for c in filtered if c['price'] <= budget]
        if fuel_type:
            filtered = [c for c in filtered if c['fuel_type'].lower() == fuel_type.lower()]
        if car_type:
            filtered = [c for c in filtered if c['type'].lower() == car_type.lower()]
        if seats:
            filtered = [c for c in filtered if c['seats'] >= seats]
        if brand:
            filtered = [c for c in filtered if brand in c['brand'].lower()]

        # Top 3 closest to budget
        if budget and budget > 0 and filtered:
            top_matches = sorted(filtered, key=lambda c: abs(c['price'] - budget))[:3]
        elif filtered:
            top_matches = filtered[:3]
        else:
            top_matches = []

        # Smart picks by category
        for cat in ['SUV', 'Sedan', 'Hatchback', 'MPV', 'Electric']:
            if cat == 'Electric':
                cat_cars = [c for c in filtered if c['fuel_type'].lower() == 'electric']
            else:
                cat_cars = [c for c in filtered if c['type'].lower() == cat.lower()]
            categories[cat] = cat_cars[0] if cat_cars else None

        # Suggest alternatives: cars that are close to budget but not in top_matches
        if budget and budget > 0:
            alt_candidates = [c for c in cars if c not in top_matches and abs(c['price'] - budget) <= 300000]
            alternatives = sorted(alt_candidates, key=lambda c: abs(c['price'] - budget))[:3]
        else:
            alternatives = []

    return render_template(
        'recommend.html',
        top_matches=top_matches,
        categories=categories,
        alternatives=alternatives
    )

if __name__ == '__main__':
    app.run(debug=True)
