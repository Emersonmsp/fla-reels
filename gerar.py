"""Vídeo vertical do Fla Manchete (1080x1920, 20-40 s), sem apresentador: cartões da marca + narração sintética + legenda.

Entrada: JSON (arquivo ou variável PAYLOAD) com
  n, quadro (URG = Fla Urgente, MER = Mercado Fla, NOT = notícia), roteiro (texto narrado, sem a pergunta), pergunta,
  e dentro de "x" (o GitHub aceita no máximo 10 campos no client_payload): cartoes (2 a 3 frases com *destaque*),
  arte (URL da arte do story da notícia), editoria, selo, capa (3 a 6 palavras pra capa do Short), yt (true = versão YouTube/TikTok).
Saída: reels.mp4 e capa.jpg na pasta de trabalho.

Linha do tempo (formatos A e B do Prompt Mestre): vinheta 1,4 s → arte da notícia → cartões de fato → "E aí, Nação?" com a pergunta.
A pergunta entra no tempo exato em que a narração chega nela (marcações de palavra do edge-tts). Sem música: só a da biblioteca da rede, no app.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.parse

import edge_tts
import requests
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1080, 1920, 30
AQUI = os.path.dirname(os.path.abspath(__file__))
VOZ = os.environ.get('VOZ', 'pt-BR-AntonioNeural')
INTRO = 1.4
Y_LEGENDA = 1390  # faixa livre nos cartões (1390 a 1620)
Y_LEGENDA_ARTE = 420  # na arte do story a manchete fica embaixo: a legenda sobe pra cima da bandeira


def chrome():
    for c in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'chrome'):
        if shutil.which(c):
            return shutil.which(c)
    win = r'C:\Program Files\Google\Chrome\Application\chrome.exe'
    if os.path.exists(win):
        return win
    raise SystemExit('Chrome não encontrado')


def cartao(params, png):
    url = 'file:///' + os.path.join(AQUI, 'arte', 'video.html').replace('\\', '/').lstrip('/') + '?' + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    subprocess.run([chrome(), '--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars', '--allow-file-access-from-files',
                    '--force-device-scale-factor=1', '--virtual-time-budget=6000', f'--window-size={W},{H}', f'--screenshot={png}', url],
                   check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return png


def baixar(url, dst):
    """Arte do story (9:16) da notícia. Outro formato é recortado pelo centro, sem esticar."""
    try:
        if os.path.exists(url):  # teste local
            im = Image.open(url)
        else:
            r = requests.get(url, timeout=30, headers={'User-Agent': 'FlaManchete/1.0'})
            if r.status_code != 200 or len(r.content) < 5000:
                return None
            open(dst + '.img', 'wb').write(r.content)
            im = Image.open(dst + '.img')
        im = im.convert('RGB')
        esc = max(W / im.width, H / im.height)
        im = im.resize((int(im.width * esc + 0.5), int(im.height * esc + 0.5)), Image.LANCZOS)
        x, y = (im.width - W) // 2, (im.height - H) // 2
        im.crop((x, y, x + W, y + H)).save(dst, quality=95)
        return dst
    except Exception as e:
        print('arte falhou', url, e)
    return None


async def narrar(texto, mp3):
    com = edge_tts.Communicate(texto, VOZ, rate=os.environ.get('VELOCIDADE', '+8%'), boundary='WordBoundary')
    palavras = []
    with open(mp3, 'wb') as f:
        async for ch in com.stream():
            if ch['type'] == 'audio':
                f.write(ch['data'])
            elif ch['type'] == 'WordBoundary':
                palavras.append((ch['offset'] / 1e7, (ch['offset'] + ch['duration']) / 1e7, ch['text']))
    return palavras


def legendas(palavras, pasta, desde):
    """Blocos de até 3 palavras (ritmo de Reels), cada um num PNG transparente com entrada e saída."""
    fonte = ImageFont.truetype(os.path.join(AQUI, 'fontes', 'Archivo-Italic.ttf'), 70)
    try:
        fonte.set_variation_by_axes([900, 118])  # peso, largura (ordem dos eixos da Archivo: Weight, Width)
    except Exception:
        pass
    blocos, atual = [], []
    for p in palavras:
        if p[0] < desde:
            continue
        atual.append(p)
        if len(atual) >= 3 or p[2].endswith(('.', ',', '?', '!', ':')):
            blocos.append(atual)
            atual = []
    if atual:
        blocos.append(atual)
    saida = []
    for i, b in enumerate(blocos):
        txt = ' '.join(w[2] for w in b).upper()
        im = Image.new('RGBA', (W, 230), (0, 0, 0, 0))
        dr = ImageDraw.Draw(im)
        larg = dr.textlength(txt, font=fonte)
        f = fonte
        linhas = [txt]
        if larg > 960:  # bloco comprido: quebra em 2 linhas no ponto que deixa as duas mais parecidas
            ws = txt.split()
            k = min(range(1, len(ws)), key=lambda i: abs(dr.textlength(' '.join(ws[:i]), font=f) - dr.textlength(' '.join(ws[i:]), font=f)), default=1)
            linhas = [' '.join(ws[:k]), ' '.join(ws[k:])] if len(ws) > 1 else [txt]
        yy = 10
        for ln in linhas:
            x = (W - dr.textlength(ln, font=f)) / 2
            dr.text((x, yy), ln, font=f, fill=(244, 244, 244), stroke_width=9, stroke_fill=(11, 11, 11))
            yy += 92
        p = os.path.join(pasta, f'leg{i}.png')
        im.save(p)
        fim = blocos[i + 1][0][0] if i + 1 < len(blocos) else b[-1][1] + 0.4
        saida.append((p, b[0][0], fim))
    return saida


def inicio_da_pergunta(palavras, roteiro):
    """Tempo em que a narração começa a pergunta (a pergunta vem logo depois das palavras do roteiro)."""
    n = len(roteiro.split())
    return palavras[n][0] if len(palavras) > n else (palavras[-1][1] if palavras else 20)


def main():
    bruto = os.environ.get('PAYLOAD') or open(sys.argv[1], encoding='utf-8').read()
    d = json.loads(bruto)
    d.update(d.pop('x', None) or {})
    pasta = os.path.abspath(os.environ.get('SAIDA', 'trabalho'))
    os.makedirs(pasta, exist_ok=True)
    quadro = str(d.get('quadro', 'NOT')).upper()
    roteiro, pergunta = d['roteiro'].strip(), (d.get('pergunta') or 'E aí, Nação? Comenta aí.').strip()
    cartoes = [c for c in (d.get('cartoes') or []) if c][:3]

    mp3 = os.path.join(pasta, 'narracao.mp3')
    palavras = asyncio.run(narrar(roteiro + ' ' + pergunta, mp3))
    if not palavras:
        raise SystemExit('narração vazia')
    fim_voz = palavras[-1][1]
    t_perg = inicio_da_pergunta(palavras, roteiro)
    total = fim_voz + 1.2

    # cartões
    base = {'quadro': quadro, 'editoria': d.get('editoria') or 'Notícia'}
    cenas = [(cartao(dict(base, tipo='intro'), os.path.join(pasta, 'c_intro.png')), INTRO)]
    arte = baixar(d['arte'], os.path.join(pasta, 'c_arte.jpg')) if d.get('arte') else None
    # Cortes presos à fala: a arte fica com ~30% das palavras do roteiro e cada cartão com uma fatia igual do resto,
    # e o corte cai no começo de uma palavra (o cartão aparece quando a voz chega naquele ponto).
    n_rot = min(len(roteiro.split()), len(palavras))
    ini = next((i for i, p in enumerate(palavras[:n_rot]) if p[0] >= INTRO - 0.2), 0)
    fatias = (['arte'] if arte else []) + list(range(len(cartoes)))
    pesos = [0.3] + [0.7 / len(cartoes)] * len(cartoes) if arte and cartoes else [1 / len(fatias)] * len(fatias) if fatias else []
    cortes, acum = [INTRO], 0.0
    for pz in pesos[:-1]:
        acum += pz
        k = min(n_rot - 1, ini + max(1, round((n_rot - ini) * acum)))
        cortes.append(max(cortes[-1] + 1.5, palavras[k][0]))
    cortes.append(max(cortes[-1] + 1.5, t_perg))
    t_perg = cortes[-1]
    janela_arte = (0, 0)
    for j, f in enumerate(fatias):
        dur = cortes[j + 1] - cortes[j]
        if f == 'arte':
            cenas.append((arte, dur))
            janela_arte = (cortes[j], cortes[j + 1])
        else:
            png = cartao(dict(base, tipo='fato', texto=cartoes[f], passo=f + 1, total=len(cartoes)), os.path.join(pasta, f'c_fato{f}.png'))
            cenas.append((png, dur))
    total = max(total, t_perg + 2.5)
    if not arte and not cartoes:
        cenas[0] = (cenas[0][0], t_perg)
    cenas.append((cartao(dict(base, tipo='pergunta', texto=pergunta.replace('E aí, Nação?', '').strip() or pergunta),
                         os.path.join(pasta, 'c_pergunta.png')), total - t_perg))
    if d.get('capa'):
        try:
            cartao(dict(base, tipo='capa', texto=d['capa'], selo=d.get('selo') or ''), os.path.join(pasta, 'capa.png'))
            Image.open(os.path.join(pasta, 'capa.png')).convert('RGB').save(os.path.join(pasta, 'capa.jpg'), quality=90)
        except Exception as e:
            print('capa falhou', e)

    # cada cena: empurrão lento de câmera (zoom 1,00 → 1,05), imagem dobrada antes pra não tremer
    entradas, filtros = [], []
    for i, (img, dur) in enumerate(cenas):
        quadros = max(1, int(round(dur * FPS)))
        entradas += ['-loop', '1', '-framerate', str(FPS), '-t', f'{dur:.3f}', '-i', img]
        filtros.append(f"[{i}:v]scale={W * 2}:{H * 2},zoompan=z='1+0.05*on/{quadros}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={quadros}:s={W}x{H}:fps={FPS},"
                       f"trim=duration={dur:.3f},setpts=PTS-STARTPTS,format=yuv420p,setsar=1[s{i}]")
    n = len(cenas)
    filtros.append(''.join(f'[s{i}]' for i in range(n)) + f'concat=n={n}:v=1:a=0[base]')
    atual = 'base'
    legs = legendas(palavras, pasta, INTRO - 0.2)
    for j, (p, t0, t1) in enumerate(legs):
        entradas += ['-loop', '1', '-t', f'{total:.3f}', '-i', p]
        y = Y_LEGENDA_ARTE if janela_arte[0] <= t0 < janela_arte[1] else Y_LEGENDA
        if janela_arte[0] <= t0 < janela_arte[1] < t1:
            t1 = janela_arte[1]  # bloco que atravessa o corte: termina no corte (o próximo cartão já tem texto embaixo)
        filtros.append(f"[{atual}][{n + j}:v]overlay=0:{y}:eof_action=pass:enable='between(t,{t0:.2f},{t1:.2f})'[v{j}]")
        atual = f'v{j}'
    entradas += ['-i', mp3]
    saida = os.path.join(pasta, 'reels.mp4')
    cmd = ['ffmpeg', '-y', '-loglevel', 'error'] + entradas + [
        '-filter_complex', ';'.join(filtros), '-map', f'[{atual}]', '-map', f'{n + len(legs)}:a',
        '-c:v', 'libx264', '-preset', 'medium', '-crf', '20', '-pix_fmt', 'yuv420p', '-r', str(FPS),
        '-af', 'apad', '-c:a', 'aac', '-b:a', '160k', '-ar', '48000', '-ac', '2', '-t', f'{total:.3f}', '-movflags', '+faststart', saida]
    subprocess.run(cmd, check=True)
    print(json.dumps({'arquivo': saida, 'duracao': round(total, 1), 'cenas': n, 'legendas': len(legs)}))


if __name__ == '__main__':
    main()
