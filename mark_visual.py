"""
mark_visual.py -- DEAD CODE as of BETA 0.3.39's mark_renderer.py rewrite.

Originally the one place the mood mark's HTML/CSS/JS was built, called by
mood_mark.py (the header icon) and toki_desktop_mark.py's old
QWebEngineView-based overlay. mood_mark.py no longer exists, and
toki_desktop_mark.py now renders via mark_renderer.py's QPainter-based
widget instead -- mark_renderer.py transcribed this file's _SVG_BODY and
mood data directly rather than importing them (see the comments there).
Nothing in the live app imports this module anymore. Left in place rather
than deleted since that's a call for the project owner to make, not one
to make silently in passing -- safe to delete once confirmed unneeded.
"""

_SVG_BODY = """
<circle id="bgCircle" cx="110" cy="110" r="100" fill="#0A0A0A" style="transition:fill 0.6s ease"/>
<g id="markGroup" style="transform-origin:110px 110px;transition:transform 0.7s cubic-bezier(.22,1,.36,1);will-change:transform">
<g class="ringOuter" id="ring1" style="transform-origin:110px 110px;transition:opacity 0.6s cubic-bezier(.22,1,.36,1),transform 0.6s cubic-bezier(.22,1,.36,1);will-change:transform,opacity"><g class="ringSpin" style="transform-origin:110px 110px"><g class="ringBreathe" style="transform-origin:110px 110px"><polygon points="110,30 178,150 42,150" fill="none" stroke="#378ADD" stroke-width="3"/></g></g></g>
<g class="ringOuter" id="ring2" style="transform-origin:110px 110px;transition:opacity 0.6s cubic-bezier(.22,1,.36,1),transform 0.6s cubic-bezier(.22,1,.36,1);will-change:transform,opacity"><g class="ringSpin" style="transform-origin:110px 110px"><g class="ringBreathe" style="transform-origin:110px 110px"><polygon points="110,190 42,70 178,70" fill="none" stroke="#185FA5" stroke-width="3"/></g></g></g>
<g class="ringOuter" id="ring3" style="transform-origin:110px 110px;transition:opacity 0.6s cubic-bezier(.22,1,.36,1),transform 0.6s cubic-bezier(.22,1,.36,1);will-change:transform,opacity"><g class="ringSpin" style="transform-origin:110px 110px"><g class="ringBreathe" style="transform-origin:110px 110px"><polygon points="110,50 165,110 110,170 55,110" fill="none" stroke="#85B7EB" stroke-width="2"/></g></g></g>
<g class="ringOuter" id="ring4" style="transform-origin:110px 110px;transition:opacity 0.6s cubic-bezier(.22,1,.36,1),transform 0.6s cubic-bezier(.22,1,.36,1);will-change:transform,opacity"><g class="ringSpin" style="transform-origin:110px 110px"><g class="ringBreathe" style="transform-origin:110px 110px"><rect x="65" y="65" width="90" height="90" fill="none" stroke="#0C447C" stroke-width="2"/></g></g></g>
<g class="ringOuter" id="ring5" style="transform-origin:110px 110px;transition:opacity 0.6s cubic-bezier(.22,1,.36,1),transform 0.6s cubic-bezier(.22,1,.36,1);will-change:transform,opacity"><g class="ringSpin" style="transform-origin:110px 110px"><g class="ringBreathe" style="transform-origin:110px 110px"><rect x="80" y="80" width="60" height="60" fill="none" stroke="#B5D4F4" stroke-width="1.6" transform="rotate(45 110 110)"/></g></g></g>
<g class="ringOuter" id="ring6" style="transform-origin:110px 110px;transition:opacity 0.6s cubic-bezier(.22,1,.36,1),transform 0.6s cubic-bezier(.22,1,.36,1);will-change:transform,opacity"><g class="ringSpin" style="transform-origin:110px 110px"><g class="ringBreathe" style="transform-origin:110px 110px"><circle cx="110" cy="110" r="72" fill="none" stroke="#042C53" stroke-width="1.5" stroke-dasharray="6 10"/></g></g></g>
<g class="ringOuter" id="ring7" style="transform-origin:110px 110px;transition:opacity 0.6s cubic-bezier(.22,1,.36,1),transform 0.6s cubic-bezier(.22,1,.36,1);will-change:transform,opacity"><g class="ringSpin" style="transform-origin:110px 110px"><g class="ringBreathe" style="transform-origin:110px 110px"><circle cx="110" cy="110" r="42" fill="none" stroke="#E24B4A" stroke-width="1.5" stroke-dasharray="2 6"/></g></g></g>
<circle id="core" cx="110" cy="110" r="12" fill="#E6F1FB" style="transform-origin:110px 110px;transition:fill 0.6s ease;will-change:transform"/>
</g>
"""

_SCRIPT = """
const moods = {
  calm:       {bg:'#050B14', core:'#B5D4F4', markScale:1.1,  dur:9,   ease:'linear',                     breathe:6,   layers:{ring2:1,ring6:1},                          palette:'#378ADD,#185FA5',          pulse:'3.5s', pulseScale:1.35},
  energetic:  {bg:'#0A0A0A', core:'#FCEBEB', markScale:0.85, dur:1.1, ease:'linear',                     breathe:0.55,layers:{ring1:1,ring2:1,ring3:1,ring4:1,ring5:1,ring6:1,ring7:1}, palette:'#E24B4A,#A32D2D,#F09595',  pulse:'0.35s', pulseScale:1.6},
  mysterious: {bg:'#08070C', core:'#042C53', markScale:1.3,  dur:12,  ease:'cubic-bezier(.2,0,.8,1)',    breathe:5,   layers:{ring3:1,ring5:1},                          palette:'#0C447C,#042C53',          pulse:'5s', pulseScale:1.25},
  playful:    {bg:'#10131A', core:'#E6F1FB', markScale:0.95, dur:1.8, ease:'cubic-bezier(.34,1.56,.64,1)',breathe:0.9, layers:{ring1:1,ring4:1,ring6:1,ring7:1},          palette:'#378ADD,#85B7EB,#B5D4F4',  pulse:'0.9s', pulseScale:1.5},
  lifeless:   {bg:'#0B0B0B', core:'#5F5E5A', markScale:0.8,  dur:16,  ease:'ease-in-out',                breathe:9,   layers:{},                                         palette:'#5F5E5A',                  pulse:'4.5s', pulseScale:1.08}
};

function setMood(name){
  var cfg = moods[name];
  if(!cfg) return;
  var mark = document.getElementById('markGroup');
  var bg = document.getElementById('bgCircle');
  var core = document.getElementById('core');
  var colors = cfg.palette.split(',');
  var rings = document.querySelectorAll('.ringOuter');

  core.style.animation = 'none';
  core.style.transition = 'transform 0.22s cubic-bezier(.11,0,.5,0)';
  core.style.transform = 'scale(1.7)';
  mark.style.transition = 'transform 0.32s cubic-bezier(.55,0,.85,.35)';
  mark.style.transform = 'scale(0.3) rotate(130deg)';

  rings.forEach(function(el, i){
    var d = i * 25;
    el.style.transition = 'opacity 0.28s ease-in ' + d + 'ms, transform 0.28s cubic-bezier(.55,0,.85,.35) ' + d + 'ms';
    el.style.opacity = '0';
    el.style.transform = 'scale(0.05) rotate(-50deg)';
  });

  setTimeout(function(){
    core.style.transition = 'transform 0.16s cubic-bezier(.7,0,1,1)';
    core.style.transform = 'scale(0.12)';
    bg.style.fill = cfg.bg;
  }, 220);

  setTimeout(function(){
    core.style.fill = cfg.core;
    core.style.transition = 'transform 0.55s cubic-bezier(.34,1.56,.64,1), fill 0.4s ease';
    core.style.transform = 'scale(1)';
    pulseStyleTag.textContent = '@keyframes corepulse{0%,100%{transform:scale(1)}50%{transform:scale(' + cfg.pulseScale + ')}}';
    core.style.animation = 'corepulse ' + cfg.pulse + ' ' + cfg.ease + ' infinite';
    mark.style.transition = 'transform 0.6s cubic-bezier(.34,1.56,.64,1)';
    mark.style.transform = 'scale(' + cfg.markScale + ') rotate(0deg)';

    var i = 0;
    rings.forEach(function(el){
      var id = el.id;
      var spin = el.querySelector('.ringSpin');
      var breathe = el.querySelector('.ringBreathe');
      var shape = el.querySelector('polygon,rect,circle');
      if(cfg.layers[id]){
        var d = i * 45;
        el.style.transition = 'opacity 0.5s cubic-bezier(.34,1.56,.64,1) ' + d + 'ms, transform 0.5s cubic-bezier(.34,1.56,.64,1) ' + d + 'ms';
        el.style.opacity = '0.85';
        el.style.transform = 'scale(1) rotate(0deg)';
        shape.setAttribute('stroke', colors[i % colors.length]);
        var durMult = 1 + i * 0.17;
        spin.style.animationDuration = (cfg.dur * durMult).toFixed(2) + 's';
        spin.style.animationTimingFunction = cfg.ease;
        spin.style.animationDirection = i % 2 === 0 ? 'reverse' : 'normal';
        var breatheDur = (cfg.breathe * (1 + i * 0.12)).toFixed(2);
        breathe.style.animationDuration = breatheDur + 's';
        breathe.style.animationDelay = (-i * breatheDur * 0.15) + 's';
        i++;
      } else {
        el.style.transition = 'opacity 0.3s ease-out, transform 0.3s ease-out';
        el.style.opacity = '0';
        el.style.transform = 'scale(0.1)';
      }
    });
  }, 400);
}

var pulseStyleTag = document.createElement('style');
document.head.appendChild(pulseStyleTag);

function playStartup(){
  var order = ['calm', 'energetic', 'mysterious', 'playful', 'lifeless', 'calm'];
  order.forEach(function(name, i){
    setTimeout(function(){ setMood(name); }, i * 850);
  });
}

setMood('calm');
"""


def build_mark_html(scale: str = "fixed", px: int = 44) -> str:
    """
    scale="fixed": <svg width="{px}" height="{px}" ...> -- for a widget
    that's never resized after creation (the header icon).
    scale="fill": <svg width="100%" height="100%" ...> -- for a widget
    that gets resized live via a QPropertyAnimation on its own geometry
    (the desktop overlay growing/shrinking as it goes active/idle).
    """
    if scale == "fill":
        svg_open = '<svg width="100%" height="100%" viewBox="0 0 220 220" preserveAspectRatio="xMidYMid meet" role="img">'
    else:
        svg_open = f'<svg width="{px}" height="{px}" viewBox="0 0 220 220" role="img">'

    return f"""<!doctype html><html><head><meta charset="utf-8">
<style>
html,body{{margin:0;padding:0;background:transparent;overflow:hidden}}
#bgWrap{{display:flex;justify-content:center;align-items:center;width:100%;height:100%;padding:0}}
.ringSpin{{animation:spin linear infinite}}
@keyframes spin{{from{{transform:rotate(0deg)}}to{{transform:rotate(360deg)}}}}
.ringBreathe{{animation:breathe ease-in-out infinite}}
@keyframes breathe{{0%,100%{{transform:scale(1)}}50%{{transform:scale(0.82)}}}}
</style></head><body>
<div id="bgWrap">
{svg_open}
{_SVG_BODY}
</svg>
</div>
<script>
{_SCRIPT}
</script>
</body></html>"""
