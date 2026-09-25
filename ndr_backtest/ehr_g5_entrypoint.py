"""Render entrypoint wrapper: imports the existing IPR app unchanged, then adds G5 batch routes."""
import os
from independent_priority_radar import app, export_authorized
from ehr_g5_batch25 import register
register(app, export_authorized)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","10000")), threaded=True)
