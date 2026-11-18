"""Vídeo v2 do Fla Manchete (1080x1920, 30 fps): animado quadro a quadro em Pillow, sem navegador.

Estrutura (pedido do Emerson, 18/11/2026: "algo novo a cada 2-3 s", foto sempre presente):
  1 GANCHO  foto em tela cheia + manchete entrando palavra por palavra, carimbo da editoria (abre já na notícia)
  2..n DADOS  um número grande por cena (conta subindo), com a foto em outro recorte
  fim  "E aí, Nação?" + pergunta curta + Segue @fla_manchete
Legenda: 1 linha, palavra por palavra, pílula preta fixa (y 1480), nunca cobre o conteúdo. Whoosh nos cortes, batida no gancho.
Áudio: narração edge-tts + efeitos sintetizados aqui (nada de terceiros).

Entrada (JSON, arquivo ou PAYLOAD; o que vier em "x" é mesclado):
  n, titulo ("*destaque*" vira faixa vermelha), editoria, foto (URL ou caminho), fonte,
  roteiro: lista de frases; a 1ª é o gancho, cada uma das seguintes vira uma cena de dado, na mesma ordem de "dados"
  dados: [{"num": "28", "de": "/30", "rotulo": "pontos nos últimos 30"}, ...]   (num pode ter + ou -; de é opcional)
  pergunta: "E aí, Nação? ..." (narrada e mostrada no fim)
Saída: reels.mp4 + capa.jpg em SAIDA (padrão trabalho/).
"""
import asyncio
import json
import math
import os
import random
import re
import subprocess
import sys
import wave

import edge_tts
import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

W, H, FPS = 1080, 1920, 30
AQUI = os.path.dirname(os.path.abspath(__file__))
VOZ = os.environ.get('VOZ', 'pt-BR-AntonioNeural')
VERM, PRETO, PAPEL, CINZA = (217, 34, 42), (11, 11, 11), (244, 244, 244), (31, 31, 31)
Y_LEG = 1480          # legenda (faixa 1440-1560); abaixo de 1620 fica a interface do Reels
TRANS = 0.26          # duração do corte (slide pra cima)


# ---------------------------------------------------------------- fontes e texto
def fonte(tam, serif=False, peso=900, larg=112):
    if serif:
        return ImageFont.truetype(os.path.join(AQUI, 'fontes', 'SourceSerif4-Italic.ttf'), tam)
    f = ImageFont.truetype(os.path.join(AQUI, 'fontes', 'Archivo-Italic.ttf'), tam)
    try:
        f.set_variation_by_axes([peso, larg])
    except Exception:
        pass
    return f


_cache = {}


def palavra(txt, tam, cor=(244, 244, 244), fundo=None, pad=(18, 6), serif=False):
    """PNG de uma palavra (com faixa de fundo opcional). Cacheado: cada quadro só cola e escala."""
    k = (txt, tam, cor, fundo, pad, serif)
    if k in _cache:
        return _cache[k]
    f = fonte(tam, serif)
    x0, y0, x1, y1 = f.getbbox(txt)
    w, h = x1 - x0 + pad[0] * 2, int(tam * 1.0) + pad[1] * 2
    im = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if fundo:
        d.rectangle((0, 0, w, h), fill=fundo)
    d.text((pad[0] - x0, pad[1] + (tam - (y1 - y0)) // 2 - y0 + int(tam * 0.02)), txt, font=f, fill=cor)
    _cache[k] = im
    return im


def colar(base, im, x, y, esc=1.0, alfa=1.0, ancora='lt'):
    if alfa <= 0.01 or esc <= 0.01:
        return
    if abs(esc - 1) > 0.005:
        im = im.resize((max(1, int(im.width * esc)), max(1, int(im.height * esc))), Image.BILINEAR)
    if alfa < 0.99:
        im = im.copy()
        im.putalpha(im.getchannel('A').point(lambda a: int(a * alfa)))
    if ancora == 'c':
        x, y = x - im.width // 2, y - im.height // 2
    elif ancora == 'ct':
        x = x - im.width // 2
    base.alpha_composite(im, (int(x), int(y)))


def quebrar(palavras, tam, larg_max):
    """Linhas de palavras que cabem em larg_max (medindo com a fonte do título)."""
    f, linhas, atual = fonte(tam), [], []
    for p in palavras:
        teste = ' '.join(w for w, _ in atual + [p])
        if atual and f.getlength(teste) > larg_max:
            linhas.append(atual)
            atual = []
        atual.append(p)
    if atual:
        linhas.append(atual)
    return linhas


# ---------------------------------------------------------------- easing
def clamp(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def out_expo(x):
    x = clamp(x)
    return 1 if x >= 1 else 1 - 2 ** (-10 * x)


def out_back(x, s=1.7):
    x = clamp(x) - 1
    return 1 + x * x * ((s + 1) * x + s)


def out_cubic(x):
    return 1 - (1 - clamp(x)) ** 3


# ---------------------------------------------------------------- foto
def parte_nitida(im):
    """Muitas fotos de portal vêm com bordas borradas (a mesma foto esticada atrás). Fica só com o miolo nítido."""
    g = np.asarray(im.convert('L').resize((im.width // 4, im.height // 4)), dtype=np.float32)
    e = np.abs(np.diff(g, axis=1)).mean(axis=0)
    e = np.convolve(e, np.ones(9) / 9, mode='same')
    lim = e.max() * 0.35
    cols = np.where(e > lim)[0]
    if len(cols) < 10:
        return im
    x0, x1 = cols[0] * 4, cols[-1] * 4
    if x1 - x0 < im.width * 0.3 or x1 - x0 > im.width * 0.92:
        return im
    return im.crop((x0, 0, x1, im.height))


def carregar_foto(src, pasta):
    if os.path.exists(src):
        im = Image.open(src)
    else:
        r = requests.get(src, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
        r.raise_for_status()
        p = os.path.join(pasta, 'foto_original')
        open(p, 'wb').write(r.content)
        im = Image.open(p)
    im = parte_nitida(im.convert('RGB'))
    # tratamento de jornal: P&B com contraste e um pouco de grão
    g = ImageOps.autocontrast(im.convert('L'), cutoff=1)
    return g


def cobrir(g, w, h, foco=(0.5, 0.3), zoom=1.0):
    """Recorte que cobre w x h (sem esticar), centrado no foco (0-1) e com zoom extra."""
    esc = max(w / g.width, h / g.height) * zoom
    gw, gh = g.width * esc, g.height * esc
    x = min(max(foco[0] * gw - w / 2, 0), gw - w)
    y = min(max(foco[1] * gh - h / 2, 0), gh - h)
    box = (x / esc, y / esc, (x + w) / esc, (y + h) / esc)
    return g.resize((w, h), Image.BICUBIC, box=box)


# ---------------------------------------------------------------- narração e áudio
async def narrar(texto, mp3):
    com = edge_tts.Communicate(texto, VOZ, rate=os.environ.get('VELOCIDADE', '+12%'), boundary='WordBoundary')
    pal = []
    with open(mp3, 'wb') as f:
        async for ch in com.stream():
            if ch['type'] == 'audio':
                f.write(ch['data'])
            elif ch['type'] == 'WordBoundary':
                pal.append([ch['offset'] / 1e7, (ch['offset'] + ch['duration']) / 1e7, ch['text']])
    return pal


SR = 48000


def whoosh(d=0.42):
    n = int(SR * d)
    t = np.linspace(0, 1, n)
    ru = np.random.default_rng(7).standard_normal(n)
    # passa-baixa que abre e fecha (o "vuuup"): média móvel de janela variável aproximada por 2 filtros misturados
    k1, k2 = np.ones(40) / 40, np.ones(6) / 6
    a, b = np.convolve(ru, k1, 'same'), np.convolve(ru, k2, 'same')
    mix = np.sin(np.pi * t) ** 2
    s = a * (1 - mix) * 2.2 + b * mix * 0.9
    env = np.where(t < 0.55, (t / 0.55) ** 2, ((1 - t) / 0.45) ** 1.5)
    return s * env * 0.32


def batida(d=0.7):
    n = int(SR * d)
    t = np.arange(n) / SR
    f = 50 + 70 * np.exp(-t * 18)
    s = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 6)
    s[:200] += np.random.default_rng(3).standard_normal(200) * np.linspace(1, 0, 200) * 0.6
    return s * 0.75


def tique(d=0.09):
    n = int(SR * d)
    t = np.arange(n) / SR
    return np.sin(2 * np.pi * 1400 * t) * np.exp(-t * 60) * 0.22


def efeitos(eventos, total, wav):
    buf = np.zeros(int(SR * (total + 1)))
    for t0, s in eventos:
        i = int(max(0, t0) * SR)
        j = min(len(buf), i + len(s))
        buf[i:j] += s[:j - i]
    buf = np.clip(buf, -1, 1)
    with wave.open(wav, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((buf * 32000).astype(np.int16).tobytes())


# ---------------------------------------------------------------- cenas
class Cena:
    def __init__(self, t0, t1):
        self.t0, self.t1 = t0, t1


def logo(tam_w):
    im = Image.open(os.path.join(AQUI, 'Logo', 'logo-v4-compacto.png')).convert('RGBA')
    return im.resize((tam_w, int(im.height * tam_w / im.width)), Image.LANCZOS)


def gradiente(h0, h1, a0, a1):
    g = Image.new('L', (1, h1 - h0))
    for y in range(h1 - h0):
        g.putpixel((0, y), int(a0 + (a1 - a0) * (y / max(1, h1 - h0 - 1)) ** 1.4))
    preto = Image.new('RGBA', (W, h1 - h0), PRETO + (0,))
    preto.putalpha(g.resize((W, h1 - h0)))
    return preto


class Gancho(Cena):
    def __init__(self, t0, t1, g, titulo, editoria, fonte_txt):
        super().__init__(t0, t1)
        self.bg = cobrir(g, int(W * 1.18), int(H * 1.18), foco=(0.5, 0.28)).convert('RGBA')
        self.grad = gradiente(700, H, 0, 245)
        self.logo = logo(300)
        self.tag = palavra(editoria.upper(), 54, PAPEL, VERM, pad=(26, 10))
        self.fonte = palavra('FONTE: ' + fonte_txt.upper(), 30, (190, 190, 190), None, pad=(0, 0))
        # palavras do título, marcando as que estão entre *asteriscos*
        pal, dest = [], False
        for tok in re.findall(r'\*|[^\s*]+', titulo):
            if tok == '*':
                dest = not dest
            else:
                pal.append((tok.upper(), dest))
        self.tam = 104 if len(' '.join(p for p, _ in pal)) <= 34 else 90
        self.linhas = quebrar(pal, self.tam, W - 150)
        self.y_tit = 1390 - len(self.linhas) * int(self.tam * 1.1)

    def quadro(self, t):
        lt = t - self.t0
        z = 1.0 + 0.12 * (1 - out_expo(lt / 0.6)) + 0.04 * clamp(lt / (self.t1 - self.t0))  # soco + aproximação lenta
        bw, bh = int(W * z), int(H * z)
        bg = self.bg.resize((bw, bh), Image.BILINEAR, box=(0, 0, self.bg.width, self.bg.height))
        f = Image.new('RGBA', (W, H), PRETO + (255,))
        f.alpha_composite(bg, ((W - bw) // 2, (H - bh) // 2 - 40))
        f.alpha_composite(self.grad, (0, 700))
        colar(f, self.logo, 60 - 300 * (1 - out_expo((lt - 0.1) / 0.5)), 110)
        colar(f, self.tag, 70, self.y_tit - 110 - 30 * (1 - out_back((lt - 0.15) / 0.35)), alfa=clamp((lt - 0.15) / 0.15))
        y, k = self.y_tit, 0
        for linha in self.linhas:
            x = 70
            for j, (p, dest) in enumerate(linha):
                junto = dest and j + 1 < len(linha) and linha[j + 1][1]  # próxima também é destaque: faixa contínua
                im = palavra(p + (' ' if junto else ''), self.tam, PAPEL, VERM if dest else None, pad=(14 if dest else 4, 4))
                ti = lt - 0.25 - k * 0.075
                colar(f, im, x + im.width / 2, y + im.height / 2, esc=1.35 - 0.35 * out_back(ti / 0.28), alfa=clamp(ti / 0.12), ancora='c')
                x += im.width + (0 if junto else 16)
                k += 1
            y += int(self.tam * 1.1)
        return f


class Dado(Cena):
    """Número grande contando + rótulo. Alterna 3 layouts pra cada cena parecer nova."""

    def __init__(self, t0, t1, g, dado, layout, n_cena):
        super().__init__(t0, t1)
        self.d, self.lay = dado, layout
        self.num = str(dado['num'])
        m = re.match(r'([+\-]?)(\d+)', self.num)
        self.sinal, self.alvo = (m.group(1), int(m.group(2))) if m else ('', 0)
        self.rot = str(dado.get('rotulo', '')).upper()
        focos = [(0.5, 0.25), (0.62, 0.2), (0.42, 0.3)]
        if layout == 0:      # papel: foto em janela no alto, número preto embaixo
            self.foto = cobrir(g, 940, 600, foco=focos[n_cena % 3], zoom=1.25).convert('RGBA')
        elif layout == 1:    # metade vermelha / metade foto
            self.foto = cobrir(g, W, 800, foco=focos[n_cena % 3], zoom=1.15).convert('RGBA')
        else:                # vermelho cheio com a foto fraca atrás
            f = cobrir(g, W, H, foco=focos[n_cena % 3], zoom=1.1).convert('RGBA')
            f.putalpha(70)
            self.foto = f

    def contador(self, lt):
        if not self.alvo and not re.search(r'\d', self.num):
            return self.num.upper()
        v = int(round(self.alvo * out_cubic((lt - 0.25) / 0.9)))
        return self.sinal + str(v)

    def quadro(self, t):
        lt = t - self.t0
        lay = self.lay
        if lay == 0:
            f = Image.new('RGBA', (W, H), PAPEL + (255,))
            d = ImageDraw.Draw(f)
            d.rectangle((70, 150, W - 70, 156), fill=PRETO)
            colar(f, palavra('FLA MANCHETE · ' + ('OS NÚMEROS' if re.search(r'\d', self.num) else 'O FATO'), 34, PRETO, None, pad=(0, 0), serif=True), 70, 172)
            dy = 60 * (1 - out_expo(lt / 0.45))
            d.rectangle((70 + 14, 250 + 14 + dy, 70 + 940 + 14, 250 + 600 + 14 + dy), fill=VERM)
            z = 1 + 0.05 * clamp(lt / (self.t1 - self.t0))
            ft = self.foto.resize((int(940 * z), int(600 * z)), Image.BILINEAR).crop((0, 0, 940, 600))
            f.alpha_composite(ft, (70, int(250 + dy)))
            cor_n, cor_de, y_n = PRETO, VERM, 880
        elif lay == 1:
            f = Image.new('RGBA', (W, H), VERM + (255,))
            dy = int(-800 * (1 - out_expo(lt / 0.5)))
            z = 1.06 - 0.06 * clamp(lt / (self.t1 - self.t0))
            ft = self.foto.resize((int(W * z), int(800 * z)), Image.BILINEAR)
            f.alpha_composite(ft.crop(((ft.width - W) // 2, 0, (ft.width - W) // 2 + W, 800)), (0, dy))
            ImageDraw.Draw(f).rectangle((0, 800 + dy, W, 812 + dy), fill=PRETO)
            cor_n, cor_de, y_n = PAPEL, PRETO, 850
        else:
            f = Image.new('RGBA', (W, H), VERM + (255,))
            z = 1.1 - 0.08 * clamp(lt / (self.t1 - self.t0))
            ft = self.foto.resize((int(W * z), int(H * z)), Image.BILINEAR)
            f.alpha_composite(ft, ((W - ft.width) // 2, (H - ft.height) // 2))
            cor_n, cor_de, y_n = PAPEL, PRETO, 520
        # número: entra com soco (escala), conta até o valor
        num = palavra(self.contador(lt), 380, cor_n, None, pad=(10, 0))
        de = palavra(self.d.get('de', ''), 170, cor_de, None, pad=(6, 0)) if self.d.get('de') else None
        larg = num.width + (de.width + 6 if de else 0)
        caber = min(1.0, (W - 140) / max(1, larg))  # palavra comprida no lugar do número: encolhe pra caber
        x0 = (W - larg * caber) / 2 if lay != 0 else 70
        e = (1.6 - 0.6 * out_back((lt - 0.1) / 0.35)) * caber
        a = clamp((lt - 0.1) / 0.12)
        colar(f, num, x0 + num.width * caber / 2, y_n + num.height / 2, esc=e, alfa=a, ancora='c')
        if de:
            colar(f, de, x0 + num.width + 6 + 300 * (1 - out_expo((lt - 0.5) / 0.4)), y_n + num.height - de.height - 30, alfa=clamp((lt - 0.5) / 0.15))
        # rótulo em 1-2 linhas, entra palavra por palavra
        pal = [(w, False) for w in self.rot.split()]
        y = y_n + num.height + 10
        k = 0
        for linha in quebrar(pal, 72, W - 140):
            larg_l = sum(palavra(p, 72, cor_n, None, pad=(4, 2)).width + 14 for p, _ in linha) - 14
            x = (W - larg_l) / 2 if lay != 0 else 70
            for p, _ in linha:
                im = palavra(p, 72, cor_n, None, pad=(4, 2))
                ti = lt - 0.55 - k * 0.06
                colar(f, im, x, y + 40 * (1 - out_expo(ti / 0.3)), alfa=clamp(ti / 0.15))
                x += im.width + 14
                k += 1
            y += 82
        # barra que cresce embaixo do rótulo
        bw = int((W - 140) * out_expo((lt - 0.8) / 0.6))
        if bw > 0:
            bx = 70 if lay == 0 else (W - bw) // 2
            ImageDraw.Draw(f).rectangle((bx, y + 12, bx + bw, y + 24), fill=cor_de if lay != 1 else PRETO)
        return f


class Final(Cena):
    def __init__(self, t0, t1, g, pergunta):
        super().__init__(t0, t1)
        b = cobrir(g, W, H, foco=(0.5, 0.25), zoom=1.05).convert('RGBA')
        b.putalpha(70)
        self.bg = b
        self.logo = logo(360)
        q = re.sub(r'^\s*E a[ií],?\s*Na[cç][aã]o\?\s*', '', pergunta, flags=re.I).strip()
        self.q = [(w.upper(), False) for w in q.split()]

    def quadro(self, t):
        lt = t - self.t0
        f = Image.new('RGBA', (W, H), PRETO + (255,))
        f.alpha_composite(self.bg)
        colar(f, palavra('E AÍ,', 170, PAPEL, None, pad=(6, 0)), 70, 360 - 80 * (1 - out_expo(lt / 0.4)), alfa=clamp(lt / 0.12))
        colar(f, palavra('NAÇÃO?', 170, PAPEL, VERM, pad=(22, 6)), 70 + 400 * (1 - out_expo((lt - 0.12) / 0.45)), 545, alfa=clamp((lt - 0.12) / 0.12))
        y, k = 790, 0
        for linha in quebrar(self.q, 80, W - 140):
            x = 70
            for p, _ in linha:
                im = palavra(p, 80, PAPEL, None, pad=(4, 2))
                ti = lt - 0.5 - k * 0.05
                colar(f, im, x, y + 30 * (1 - out_expo(ti / 0.3)), alfa=clamp(ti / 0.15))
                x += im.width + 14
                k += 1
            y += 92
        ti = lt - 1.2
        colar(f, palavra('COMENTA AÍ', 52, PRETO, PAPEL, pad=(28, 12)), 70, y + 40, esc=0.6 + 0.4 * out_back(ti / 0.3), alfa=clamp(ti / 0.1))
        tl = lt - 1.6
        colar(f, self.logo, W / 2, 1220 + 40 * (1 - out_expo(tl / 0.4)), alfa=clamp(tl / 0.2), ancora='ct')
        colar(f, palavra('SEGUE @FLA_MANCHETE', 46, PAPEL, None, pad=(0, 0)), W / 2, 1220 + self.logo.height + 20, alfa=clamp((tl - 0.2) / 0.2), ancora='ct')
        return f


# ---------------------------------------------------------------- legenda
def blocos_legenda(pal, inicios):
    blocos, atual = [], []
    for i, p in enumerate(pal):
        if i in inicios and atual:
            blocos.append(atual)
            atual = []
        atual.append(p)
        if len(atual) >= 3 or re.search(r'[.,?!:]$', p[2]):
            blocos.append(atual)
            atual = []
    if atual:
        blocos.append(atual)
    return blocos


def legenda(f, blocos, t):
    for i, b in enumerate(blocos):
        fim = blocos[i + 1][0][0] if i + 1 < len(blocos) else b[-1][1] + 0.3
        if not (b[0][0] <= t < fim):
            continue
        lt = t - b[0][0]
        ims = []
        for w in b:
            ativo = w[0] <= t
            ims.append(palavra(w[2].upper().strip('.,'), 62, VERM if ativo and w is max((x for x in b if x[0] <= t), key=lambda x: x[0]) else PAPEL, None, pad=(2, 0)))
        larg = sum(im.width for im in ims) + 18 * (len(ims) - 1)
        alt = max(im.height for im in ims)
        e = (1.0 + 0.08 * (1 - out_expo(lt / 0.15))) * min(1.0, 960 / (larg + 56))
        pil = Image.new('RGBA', (larg + 56, alt + 28), (0, 0, 0, 0))
        ImageDraw.Draw(pil).rounded_rectangle((0, 0, pil.width, pil.height), radius=18, fill=PRETO + (235,))
        x = 28
        for im in ims:
            pil.alpha_composite(im, (x, 14))
            x += im.width + 18
        colar(f, pil, W / 2, Y_LEG + pil.height / 2, esc=e, ancora='c')
        return


def _norm(w):
    return re.sub(r'[^\wÀ-ú]', '', w.lower())


def inicio_frases(pal, frases):
    """Índice (em pal) da 1ª palavra de cada frase. Alinha as palavras da voz com as do texto, porque o edge-tts às vezes junta
    ou separa palavras ("E aí", números): procura cada palavra do texto até 4 posições à frente."""
    txt = []
    for i, s in enumerate(frases):
        ws = [_norm(w) for w in s.split() if _norm(w)]
        txt += [(w, i if j == 0 else None) for j, w in enumerate(ws)]
    dono = [x[1] for x in txt]
    marca, k = {}, 0
    for ti, (w, _) in enumerate(txt):
        for a in range(k, min(k + 4, len(pal))):
            if _norm(pal[a][2]) == w:
                if dono[ti] is not None and dono[ti] not in marca:
                    marca[dono[ti]] = a
                k = a + 1
                break
    out, ult = [], 0
    for i in range(len(frases)):
        ult = marca.get(i, ult if i == 0 else min(ult + 1, len(pal) - 1))
        out.append(ult)
    out[0] = 0
    return out


# ---------------------------------------------------------------- montagem
def main():
    bruto = os.environ.get('PAYLOAD') or open(sys.argv[1], encoding='utf-8').read()
    d = json.loads(bruto)
    d.update(d.pop('x', None) or {})
    pasta = os.path.abspath(os.environ.get('SAIDA', 'trabalho'))
    os.makedirs(pasta, exist_ok=True)
    if not d.get('foto'):
        raise SystemExit('sem foto: o vídeo não sai (regra do Emerson, 01/11/2026)')
    g = carregar_foto(d['foto'], pasta)
    frases = [s.strip() for s in d['roteiro']]
    dados = (d.get('dados') or [])[:max(0, len(frases) - 1)]
    pergunta = d.get('pergunta') or 'E aí, Nação? Comenta aí.'

    mp3 = os.path.join(pasta, 'narracao.mp3')
    pal = asyncio.run(narrar(' '.join(frases + [pergunta]), mp3))
    if not pal:
        raise SystemExit('narração vazia')
    # o edge-tts junta número + palavra ("28 pontos") numa marcação só: separa, dividindo o tempo
    sep = []
    for t0, t1, txt in pal:
        ws = txt.split()
        for j, w in enumerate(ws):
            sep.append([t0 + (t1 - t0) * j / len(ws), t0 + (t1 - t0) * (j + 1) / len(ws), w])
    pal = sep
    # começo de cada frase = 1ª palavra dela na narração (conta as palavras de cada frase)
    idx = inicio_frases(pal, frases + [pergunta])
    ini = [pal[i][0] for i in idx]
    idx = set(idx)
    total = pal[-1][1] + 1.3
    cortes = [0.0] + [max(0.0, t - 0.12) for t in ini[1:]] + [total]
    # frase sem dado (sobrou) fica no gancho
    n_dados = len(dados)
    cenas = [Gancho(0, cortes[1] if n_dados else cortes[-2], g, d['titulo'], d.get('editoria') or 'Notícia', d.get('fonte') or '')]
    for i, dd in enumerate(dados):
        cenas.append(Dado(cortes[i + 1], cortes[i + 2], g, dd, [0, 1, 2][i % 3], i))
    cenas.append(Final(cortes[len(dados) + 1] if n_dados else cortes[-2], total, g, pergunta))
    for i in range(len(cenas) - 1):
        cenas[i].t1 = cenas[i + 1].t0

    sons = [(0.0, batida())] + [(c.t0 - 0.2, whoosh()) for c in cenas[1:]]
    for c in cenas:
        if isinstance(c, Dado):
            for j in range(6):
                sons.append((c.t0 + 0.25 + j * 0.15, tique()))
    wav = os.path.join(pasta, 'efeitos.wav')
    efeitos(sons, total, wav)
    blocos = blocos_legenda(pal, idx)

    saida = os.path.join(pasta, 'reels.mp4')
    ff = subprocess.Popen(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{W}x{H}', '-r', str(FPS), '-i', '-',
                           '-i', mp3, '-i', wav, '-filter_complex', '[1:a]volume=1.0,apad[v];[2:a]volume=0.9[s];[v][s]amix=inputs=2:duration=first:normalize=0[a]',
                           '-map', '0:v', '-map', '[a]', '-c:v', 'libx264', '-preset', os.environ.get('PRESET', 'medium'), '-crf', '19', '-pix_fmt', 'yuv420p',
                           '-c:a', 'aac', '-b:a', '160k', '-ar', '48000', '-t', f'{total:.3f}', '-movflags', '+faststart', saida], stdin=subprocess.PIPE)
    n = int(total * FPS)
    random.seed(1)
    for q in range(n):
        t = q / FPS
        i = max(j for j, c in enumerate(cenas) if c.t0 <= t)
        f = cenas[i].quadro(t)
        # corte: a cena nova sobe por cima da anterior (que sobe um pouco e escurece)
        if i > 0 and t - cenas[i].t0 < TRANS:
            p = out_expo((t - cenas[i].t0) / TRANS)
            ant = cenas[i - 1].quadro(t)
            base = Image.new('RGBA', (W, H), PRETO + (255,))
            base.alpha_composite(ant, (0, int(-420 * p)))
            base.alpha_composite(Image.new('RGBA', (W, H), (0, 0, 0, int(160 * p))))
            base.alpha_composite(f, (0, int(H * (1 - p))))
            f = base
        legenda(f, blocos, t)
        ff.stdin.write(f.convert('RGB').tobytes())
        if q == int(0.9 * FPS):
            f.convert('RGB').save(os.path.join(pasta, 'capa.jpg'), quality=90)
    ff.stdin.close()
    ff.wait()
    if ff.returncode:
        raise SystemExit('ffmpeg falhou')
    print(json.dumps({'arquivo': saida, 'duracao': round(total, 1), 'cenas': len(cenas)}))


if __name__ == '__main__':
    main()
