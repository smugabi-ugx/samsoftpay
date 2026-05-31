# Run Samsoftpay using the project venv (avoids system Python 3.14 conflict)
.venv\Scripts\python.exe -m flask --app run.py run --debug --host=127.0.0.1 --port=5000
