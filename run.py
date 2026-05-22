"""Run entry — `flask --app run.py run` picks this up."""
from app import create_app

app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
