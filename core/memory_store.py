# memory_store.py - Basic memory storage for WorldWave
import json, os

MEMORY_FILE = os.path.expanduser('~/.ww_memory.json')

def store(content, category='general', tags='', importance=0.5):
    mem = {'content': content, 'category': category, 'tags': tags, 'importance': importance, 'timestamp': __import__('time').time()}
    data = {}
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE) as f:
            data = json.load(f)
    data[str(len(data)+1)] = mem
    with open(MEMORY_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    return {'status': 'stored', 'id': str(len(data))}

def search(query, limit=5):
    if not os.path.exists(MEMORY_FILE):
        return []
    with open(MEMORY_FILE) as f:
        data = json.load(f)
    results = []
    for k, v in data.items():
        if query.lower() in v['content'].lower():
            results.append({'id': k, **v})
    return results[:limit]

def stats():
    if not os.path.exists(MEMORY_FILE):
        return {'count': 0, 'size_bytes': 0}
    size = os.path.getsize(MEMORY_FILE)
    with open(MEMORY_FILE) as f:
        count = len(json.load(f))
    return {'count': count, 'size_bytes': size}
