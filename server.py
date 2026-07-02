from flask import Flask
from routes import bp

app = Flask(__name__, static_folder='public')
app.register_blueprint(bp)

if __name__ == '__main__':
    app.run(port=3017, debug=False)
