import urllib.request, json, sys, time
BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://localhost:8080'

def test(name, payload):
    t0 = time.time()
    body = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + '/webhook/feishu', data=body, method='POST')
    try:
        r = urllib.request.urlopen(req, timeout=120)
        d = json.loads(r.read())
        ok = r.status == 200
        if ok:
            print('[PASS] ' + name + ' (' + str(round(time.time()-t0,1)) + 's)')
        else:
            print('[FAIL] ' + name + ' (' + str(round(time.time()-t0,1)) + 's)')
        if 'text' in d: print('  reply: ' + d['text'][:100])
        if 'error' in d: print('  error: ' + d['error'] + ': ' + str(d.get('detail',''))[:150])
    except urllib.error.HTTPError as e:
        print('[FAIL] ' + name + ' HTTP ' + str(e.code))
        print('  body: ' + e.read().decode('utf-8', errors='replace')[:200])

print('=== MoA Gateway Test ===')
print('Target: ' + BASE)
print()
r = urllib.request.urlopen(BASE + '/health', timeout=5)
print('[PASS] health: ' + str(json.loads(r.read())))
print()
test('greeting', {'session_id':'t1','text':'hello','message_id':'m1'})
test('coding', {'session_id':'t2','text':'write a python function','message_id':'m2'})
test('cancel', {'session_id':'t3','text':'cancel','message_id':'m3'})
print()
print('=== Done ===')