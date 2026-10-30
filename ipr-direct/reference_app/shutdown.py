class GracefulDrain:
 def __init__(self):self.state="RUNNING"
 def begin(self):self.state="DRAINING";return self.state
 def new_entries_allowed(self):return self.state=="RUNNING"
 def complete(self):self.state="STOPPED";return self.state
