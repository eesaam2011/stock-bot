from operational_priority_radar import WorkerConfig
def render_contract():
 return {"service_type":"Background Worker","start_command":"python operational_priority_radar.py",
         "filesystem_durable":False,"canonical_state":"Redis",
         "initial_mode":"OPERATIONAL_SHADOW_MODE"}
if __name__=="__main__":
 cfg=WorkerConfig.from_env();cfg.validate()
 print("OPERATIONAL_SHADOW_MODE ready; integration runtime requires injected production components")
