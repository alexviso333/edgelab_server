import subprocess, os, time

local = r'data\edgelab.sqlite'
remote = '/app/data/edgelab.sqlite'
chunk_size = 40 * 1024 * 1024  # 40MB

size = os.path.getsize(local)
print(f'Total: {size/1024/1024:.1f} MB')

def vm_up():
    r = subprocess.run('fly ssh console -C "echo ok"', shell=True, capture_output=True, text=True, timeout=30)
    return r.returncode == 0 and 'ok' in r.stdout

def wait_vm():
    for _ in range(20):
        try:
            if vm_up():
                return True
        except:
            pass
        print('  VM no disponible, esperando 15s...')
        subprocess.run('fly machine start', shell=True, capture_output=True)
        time.sleep(15)
    return False

with open(local, 'rb') as f:
    offset = 0
    i = 0
    while True:
        data = f.read(chunk_size)
        if not data:
            break
        flag = '>' if i == 0 else '>>'
        print(f'Chunk {i+1}: {offset/1024/1024:.0f}-{(offset+len(data))/1024/1024:.0f} MB...')
        if not wait_vm():
            print('ERROR: VM no arranca')
            break
        cmd = f'fly ssh console -C "sh -c \'cat {flag} {remote}\'"'
        r = subprocess.run(cmd, input=data, shell=True, timeout=120)
        if r.returncode not in (0, 1):
            print(f'  Error en chunk {i+1}, reintentando...')
            time.sleep(10)
            continue
        offset += len(data)
        i += 1
        print(f'  OK - {offset/1024/1024:.1f} MB subidos')
        time.sleep(3)

print('Verificando...')
subprocess.run('fly ssh console -C "ls -lh /app/data/edgelab.sqlite"', shell=True)
