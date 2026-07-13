@echo off
cd /d "C:\Users\iwasa\Documents\kaikei-checker\backend"
"C:\Users\iwasa\AppData\Local\Programs\Python\Python312\python.exe" -m uvicorn main:app --host 0.0.0.0 --port 8000
