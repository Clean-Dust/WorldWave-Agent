import os, sys, time, logging
logging.basicConfig(level=logging.INFO)
os.environ["WW_BOOTSTRAP_URLS"] = "http://tracker.dse-5-star-star.org"
from core.subconscious import Subconscious
s = Subconscious(enabled=True, blockchain_enabled=False)
time.sleep(5)
print(f"p2p={s.p2p is not None}, dht={s.dht is not None}, gossip={s.gossip is not None}")
if s.p2p:
    print(f"peers={s.p2p.peers_discovered}, bootstrap_urls={s.p2p.bootstrap_urls}")
    s.p2p.stop()
    print("STOPPED")
else:
    print("NO_P2P")
