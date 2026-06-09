import subprocess, sys
proc = subprocess.Popen(
    [r'C:\Users\iwasa\AppData\Local\Programs\Python\Python312\python.exe', '-m', 'uvicorn', 'main:app', '--host', '0.0.0.0', '--port', '8000'],
    stdout=open(r'C:/Users/iwasa/Documents/kaikei-checker/server_all.log', 'w', encoding='utf-8'),
    stderr=subprocess.STDOUT
)
proc.wait()
