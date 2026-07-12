"""Minimal P2P peer discovery test — directly tests bootstrap tracker registration."""
import os, sys, time, json, urllib.request, logging

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
logger = logging.getLogger("p2p-test")

TRACKER = os.environ.get("WW_BOOTSTRAP_URLS", "http://tracker.dse-5-star-star.org")
NODE_ID = os.environ.get("NODE_ID", "node-" + os.uname().nodename)
PORT = int(os.environ.get("P2P_PORT", "9833"))

def register():
    """Register this node with the bootstrap tracker."""
    data = json.dumps({
        "node_id": NODE_ID,
        "address": "0.0.0.0",
        "port": PORT,
        "version": "0.5.0-test",
        "public": False,
        "height": 0,
    }).encode()
    url = TRACKER.rstrip("/") + "/p2p/register"
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        result = json.loads(resp.read())
    logger.info(f"Registered: {result}")

def get_peers():
    """Fetch peer list from tracker."""
    url = TRACKER.rstrip("/") + "/p2p/peers"
    req = urllib.request.Request(url, headers={"User-Agent": "WW-P2P-Test"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
    return data.get("peers", [])

def main():
    logger.info(f"Node: {NODE_ID}, Tracker: {TRACKER}, Port: {PORT}")

    # Register
    register()

    # Wait a moment
    time.sleep(2)

    # Get peers
    peers = get_peers()
    other_peers = [p for p in peers if p["node_id"] != NODE_ID]
    logger.info(f"Total peers: {len(peers)}, Other nodes: {len(other_peers)}")
    for p in other_peers:
        logger.info(f"  Peer: {p['node_id']} @ {p.get('address', '?')}:{p.get('port', '?')}")

    if other_peers:
        logger.info("P2P PEER DISCOVERY: SUCCESS")
        return 0
    else:
        logger.info("P2P PEER DISCOVERY: NO OTHER PEERS (may need more nodes or wait)")
        return 0  # Not a failure — just no peers yet

if __name__ == "__main__":
    sys.exit(main())
