RUNTIME_MODE="SHADOW"
SHADOW_MODE=True
RELEASE_ID="OPERATIONAL_V1_STEP16G_SHADOW_PROVENANCE_2026-09-17"
def stamp(record):
 r=dict(record)
 r["shadow_mode"]=SHADOW_MODE
 r["runtime_mode"]=RUNTIME_MODE
 r["release_id"]=RELEASE_ID
 return r
