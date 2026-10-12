"""Render a clearly labelled design mockup from dashboard grid positions (not live evidence)."""
import html
import json
import textwrap
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont

ROOT=Path(__file__).resolve().parents[1]

def main():
    data=json.loads((ROOT/'k8s/monitoring/grafana/dashboards/mlops_v2_infra.json').read_text(encoding='utf-8'))
    width=1680; scale=67; row=28; top=160; height=top+94*row+40
    image=Image.new('RGB',(width,height),'#101217');draw=ImageDraw.Draw(image)
    fonts={n:ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf',n) for n in (13,16,19,27)}
    svg=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="#101217"/>']
    def rect(box,fill,outline='#343945'):
        draw.rectangle(box,fill=fill,outline=outline)
        x,y,x2,y2=box;svg.append(f'<rect x="{x}" y="{y}" width="{x2-x}" height="{y2-y}" rx="4" fill="{fill}" stroke="{outline}"/>')
    def text(x,y,label,size=16,color='#D8DEE9'):
        draw.text((x,y),label,font=fonts[size],fill=color)
        svg.append(f'<text x="{x}" y="{y+size}" font-family="Segoe UI,sans-serif" font-size="{size}" fill="{color}">{html.escape(label)}</text>')
    text(34,20,data['title'],27)
    text(34,59,'DESIGN MOCKUP — NOT A LIVE GRAFANA SCREENSHOT',16,'#FF9830')
    text(34,88,'Prometheus  /  production  /  credit-risk  /  All pods  /  All nodes  /  Stable + Canary  /  $0.0528 per node-hour',16)
    text(34,118,'Stable: green     Canary: amber     Warning: orange     Failure: red     Missing: NO DATA',13,'#8E8E8E')
    for p in data['panels']:
        g=p['gridPos'];x=34+g['x']*scale;y=top+g['y']*row;w=g['w']*scale-10;h=g['h']*row-10
        rect((x,y,x+w,y+h),'#181B22')
        titles=textwrap.wrap(p['title'],max(18,int(w/10)))
        for i,line in enumerate(titles):text(x+12,y+10+i*22,line,16)
        inner=y+22*len(titles)+24
        if p['type']=='text':
            for i,line in enumerate(['Runbook · evidence · rollout verification','NO DATA is not healthy. ISVC Ready alone is insufficient.','EC2 compute only — excludes EKS / EBS / transfer / tax / T3 credits.']):text(x+16,inner+i*25,line,16)
        elif p['type']=='stat':text(x+14,inner,'NO DATA',19,'#8E8E8E')
        elif p['type']=='table':
            for i in range(4):
                yy=inner+i*27
                if yy+22<y+h: rect((x+12,yy,x+w-12,yy+22),'#20252E','#292E38')
            text(x+22,inner+3,'Awaiting verified source',13,'#8E8E8E')
        else:
            for i in range(3):
                yy=inner+20+i*max(15,int((h-(inner-y)-40)/3))
                if yy<y+h-15:
                    draw.line((x+15,yy,x+w-15,yy),fill='#303642');svg.append(f'<line x1="{x+15}" y1="{yy}" x2="{x+w-15}" y2="{yy}" stroke="#303642"/>')
            text(x+22,inner+20,'NO DATA — no invented samples',13,'#8E8E8E')
    svg.append('</svg>');out=ROOT/'docs/assets';out.mkdir(parents=True,exist_ok=True)
    image.save(out/'mlops_v2_dashboard_mockup.png')
    (out/'mlops_v2_dashboard_mockup.svg').write_text('\n'.join(svg),encoding='utf-8')

if __name__=='__main__':main()
