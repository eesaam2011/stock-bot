import sys,socket,unittest
from pathlib import Path
root=Path(__file__).resolve().parent;sys.path[:0]=[str(root),str(root/'reference_app'),str(root/'tests')];sys.dont_write_bytecode=True
def deny(*a,**k):raise AssertionError('NETWORK_FORBIDDEN')
socket.socket.connect=deny;socket.socket.connect_ex=deny;socket.create_connection=deny;socket.getaddrinfo=deny
r=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover(str(root/'tests')))
raise SystemExit(0 if r.wasSuccessful() else 1)
