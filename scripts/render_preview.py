"""Render the actual terminal text for visual QA; Pillow is only a local preview tool."""
from pathlib import Path
import re
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image, ImageDraw, ImageFont
from dev_auto.ui import TerminalUI, ANSI, cells
from dev_auto.settings import DEFAULTS

root = Path(__file__).resolve().parent.parent
output = root / 'docs/validation'
output.mkdir(parents=True, exist_ok=True)
lines = []
ui = TerminalUI(write=lines.append, color=True, width=108)
ui.header('~/Projetos/meu-projeto')
ui.summary(DEFAULTS)
ui.line('PRONTO. O que vamos construir?', 'bright')
ui.line('/help comandos  /config ajustar  /status sessão', 'muted')
lines += ['', ui.tint('  você › implemente autenticação e valide os testes', 'bright')]
name = 'devswitch-preview'
if '--monitor' in sys.argv:
    from dev_auto.dashboard import quota_rows
    name = 'devswitch-monitor-preview'
    lines = []
    ui = TerminalUI(write=lines.append, color=True, width=108)
    ui.rule('painel de cotas / exemplo simulado')
    ui.event('Claude atingiu o limite. Contexto salvo; Codex assumiu a próxima etapa.')
    ui.event('Codex implementando testes; alterações anteriores preservadas.')
    now = time.time()
    sample = {'status':'running','active_agent':'codex','step':4,'created_at':now-832,
              'settings':DEFAULTS,'availability':{
                  'claude':{'status':'limited','used_percent':100,'retry_at':now+965,'quota_observed_at':now-34},
                  'codex':{'status':'available','used_percent':42,'resets_at':now+4200,'quota_observed_at':now-8}}}
    ui.rule()
    for row in quota_rows(sample, now=now):
        ui.line(row, 'bright' if row.startswith('COTAS') else 'green')
    ui.rule()
    ui.line('Valores ilustrativos. Sem percentual oficial: % não informado.', 'muted')
font = ImageFont.truetype('/System/Library/Fonts/Menlo.ttc', 17)
advance = font.getlength('M')
height = 25
image = Image.new('RGB', (int(112 * advance), (len(lines) + 3) * height), '#090f0b')
draw = ImageDraw.Draw(image)
def draw_text(x, y, text, color):
    for char in text:
        value = ord(char) - 0x2800
        if 0 <= value <= 255:
            for dx, dy, bit in [(0,0,0),(0,1,1),(0,2,2),(1,0,3),(1,1,4),(1,2,5),(0,3,6),(1,3,7)]:
                if value & (1 << bit):
                    cx, cy = x + 2 + dx * advance * .5, y + 5 + dy * 4.5
                    draw.ellipse((cx-1,cy-1,cx+1,cy+1),fill=color)
        else:
            draw.text((x,y),char,font=font,fill=color)
        x += cells(char) * advance
    return x
for row, line in enumerate(lines):
    x, color, cursor = advance, '#94e797', 0
    for match in ANSI.finditer(line):
        text = line[cursor:match.start()]
        if text:
            x = draw_text(x, (row + 1) * height, text, color)
        code = match[0]
        rgb = re.search(r'38;2;(\d+);(\d+);(\d+)', code)
        color = tuple(map(int, rgb.groups())) if rgb else '#94e797'
        cursor = match.end()
    draw_text(x, (row + 1) * height, line[cursor:], color)
image.save(output / f'{name}.png')
(output / f'{name}.txt').write_text('\n'.join(ANSI.sub('', line) for line in lines) + '\n')
print(output / f'{name}.png')
