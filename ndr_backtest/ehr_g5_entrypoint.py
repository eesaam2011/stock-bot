"""Render wrapper.

independent_priority_radar imports and registers the G5 routes itself.
This wrapper deliberately does not register them a second time.
"""
import os
from independent_priority_radar import app

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), threaded=True)
