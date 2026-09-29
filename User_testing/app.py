import os
import json
import logging
from http.server import SimpleHTTPRequestHandler, HTTPServer
import threading

logging.basicConfig(level=logging.ERROR)

message_log = []

class MessageTrackerHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass # Suppress logs

    def do_GET(self):
        if self.path == '/':
            self.path = '/index.html'
            return super().do_GET()
        elif self.path == '/api/messages':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"messages": message_log}).encode('utf-8'))
        else:
            self.send_error(404)

    def do_POST(self):
        global message_log
        if self.path == '/api/add_message':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            if post_data:
                data = json.loads(post_data.decode('utf-8'))
                message_log.append(data)
                if len(message_log) > 500:
                    message_log.pop(0)
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            
        elif self.path == '/api/reset':
            message_log = []
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_error(404)

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print("🎙️ Message Tracker GUI running at http://localhost:8765")
    server = HTTPServer(("0.0.0.0", 8765), MessageTrackerHandler)
    server.serve_forever()
