"""Artes do Fla Manchete (feed 1080x1350 e story 1080x1920), a partir de arte/arte.html.

Payload (JSON em PAYLOAD): n, modelo (URG | NOT | MER | SNOT | SURG), titulo (com *destaque*), editoria, data, fonte,
e dentro de "x" (o GitHub recusa client_payload com mais de 10 campos): selo, fina, foto (url), credito, foco.
Sai em SAIDA/arte-<n>-<modelo>.jpg. Usa o Chrome que já vem no runner do GitHub.
"""
import json
import os
import shutil
import subprocess
import urllib.parse
import urllib.request

from PIL import Image

AQUI = os.path.dirname(os.path.abspath(__file__))
STORY = {'SNOT', 'SURG'}
CAMPOS = ('titulo', 'fina', 'editoria', 'data', 'fonte', 'selo', 'n', 'credito', 'foco')


def baixar_foto(url, destino):
    try:
        # Wikimedia e alguns sites recusam pedido sem User-Agent identificado
        req = urllib.request.Request(url, headers={'User-Agent': 'FlaManchete/1.0 (github.com/Emersonmsp/fla-reels)'})
        with urllib.request.urlopen(req, timeout=30) as r:
            tipo = r.headers.get('content-type', '')
            if not tipo.startswith('image') and not url.lower().split('?')[0].endswith(('.jpg', '.jpeg', '.png', '.webp')):
                return False
            bruto = destino + '.img'
            open(bruto, 'wb').write(r.read())
        Image.open(bruto).convert('RGB').save(destino, quality=95)
        return True
    except Exception as e:
        print('foto falhou', url, e)
        return False


def chrome():
    for c in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'chrome'):
        if shutil.which(c):
            return shutil.which(c)
    raise SystemExit('Chrome não encontrado')


def main():
    p = json.loads(os.environ['PAYLOAD'])
    p.update(p.pop('x', None) or {})
    saida = os.path.abspath(os.environ.get('SAIDA', 'trabalho'))
    os.makedirs(saida, exist_ok=True)
    n, modelo = p['n'], str(p.get('modelo', 'NOT')).upper()

    params = {'modelo': modelo}
    params.update({k: str(p[k]) for k in CAMPOS if p.get(k) not in (None, '')})
    foto = os.path.join(AQUI, 'arte', 'foto.jpg')
    # Regra do Emerson (01/11): sem foto não existe arte; o robô não recebe a imagem e a pauta não vai pro grupo.
    if not (p.get('foto') and baixar_foto(p['foto'], foto)):
        raise SystemExit('sem foto: arte não gerada')
    params['foto'] = 'foto.jpg'

    alt = 1920 if modelo in STORY else 1350
    renderizar(os.path.join(AQUI, 'arte', 'arte.html'), params, 1080, alt, os.path.join(saida, f'arte-{n}-{modelo}.jpg'))


def renderizar(html, params, larg, alt, jpg):
    url = 'file://' + html + '?' + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    png = os.path.join(os.path.dirname(jpg), 'arte.png')
    subprocess.run([chrome(), '--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars',
                    '--allow-file-access-from-files', '--force-device-scale-factor=1', '--virtual-time-budget=6000',
                    f'--window-size={larg},{alt}', f'--screenshot={png}', url], check=True, timeout=120)
    Image.open(png).convert('RGB').save(jpg, quality=90, optimize=True)
    print('ok', jpg)


if __name__ == '__main__':
    main()
