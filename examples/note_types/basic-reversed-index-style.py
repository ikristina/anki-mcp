import json,urllib.request
def ac(a,**p):
    r=json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8765",json.dumps({"action":a,"version":6,"params":p}).encode())))
    if r["error"]:raise SystemExit(r["error"])
    return r["result"]
M="Basic (and reversed card)"
snd='{{#Audio}}<div class="snd">{{Audio}}</div>{{/Audio}}'
T={
 "Card 1":{
  "Front":'<div class="tag">Card 1 · Front</div>\n<div class="es">{{Front}}</div>\n'+snd,
  "Back":'<div class="tag">Card 1 · Back</div>\n<div class="es ref">{{Front}}</div>\n<hr id="answer">\n<div class="en ans">{{Back}}</div>\n'+snd},
 "Card 2":{
  "Front":'<div class="tag">Card 2 · Front</div>\n<div class="en ans">{{Back}}</div>',
  "Back":'<div class="tag">Card 2 · Back</div>\n<div class="en ref">{{Back}}</div>\n<hr id="answer">\n<div class="es">{{Front}}</div>\n'+snd}}
CSS="""/* Index look: white card, one red dot, light type. Spanish medium weight, English lighter. */
.card { font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; font-size: 20px; line-height: 1.3; text-align: left; color: #17181a; background: #ffffff; max-width: 34rem; margin: 0 auto; padding: 22px 24px; }
.tag { color: #8a8d92; font-size: 11px; letter-spacing: .14em; text-transform: uppercase; display: flex; align-items: center; gap: 8px; margin-bottom: 32px; }
.tag::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: #e0261b; }
.es { font-weight: 500; font-size: 1.3em; line-height: 1.3; letter-spacing: -.01em; }
.en { font-weight: 400; font-size: 1.15em; color: #6a6d72; }
.en.ans { color: #17181a; }
.ref { font-size: .95em; }
.es.ref { color: #8a8d92; }
hr#answer { border: 0; border-top: 1px solid #17181a; width: 28px; margin: 18px 0; }
.snd { margin-top: 22px; }
.replay-button svg { width: 30px; height: 30px; }
.replay-button svg circle { fill: #e0261b; stroke: #e0261b; }
.replay-button svg path { fill: #ffffff; stroke: #ffffff; }
.nightMode .card, .night_mode .card { color: #ececee; background: #141516; }
.nightMode .tag, .night_mode .tag { color: #8f949c; }
.nightMode .en, .night_mode .en { color: #9ba0a8; }
.nightMode .en.ans, .night_mode .en.ans { color: #ececee; }
.nightMode .es.ref, .night_mode .es.ref { color: #8f949c; }
.nightMode hr#answer, .night_mode hr#answer { border-top-color: #ececee; }
"""
ac("updateModelTemplates",model={"name":M,"templates":T})
ac("updateModelStyling",model={"name":M,"css":CSS})
print(ac("modelTemplates",modelName=M)["Card 1"]["Back"])
