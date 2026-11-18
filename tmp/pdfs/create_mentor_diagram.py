from pathlib import Path
from math import atan2, cos, sin, pi
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from reportlab.pdfbase.pdfmetrics import stringWidth

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'output' / 'pdf'
OUT.mkdir(parents=True, exist_ok=True)
PDF = OUT / 'hid-kiosk-pipeline.pdf'
W, H = 1600, 1220
C = {
    'ink': '#142B45', 'muted': '#53687D', 'blue': '#126AAB',
    'pale': '#EFF6FC', 'line': '#B9CBDC', 'white': '#FFFFFF',
    'green': '#157348', 'greenbg': '#EAF7EF',
    'amber': '#936000', 'amberbg': '#FFF5DC',
    'red': '#B52E35', 'redbg': '#FFF0F0',
    'purple': '#70509D', 'purplebg': '#F3EFF9', 'graybg': '#F4F6F8'
}
c = canvas.Canvas(str(PDF), pagesize=(W,H), pageCompression=1)
c.setTitle('HID - Can You Spot a Spoof? | Proposed kiosk pipeline')
c.setAuthor('HID Hackathon Team')
c.setSubject('Mentor overview, implementation alternatives and validation gates')

def rect(x,y,w,h,fill='white',stroke='line',radius=13):
    c.setFillColor(HexColor(C.get(fill,fill)))
    c.setStrokeColor(HexColor(C.get(stroke,stroke)))
    c.setLineWidth(1)
    c.roundRect(x,H-y-h,w,h,radius,fill=1,stroke=bool(stroke))

def txt(x,y,s,size=16,color='ink',bold=False,align='left'):
    c.setFont('Helvetica-Bold' if bold else 'Helvetica',size)
    c.setFillColor(HexColor(C.get(color,color)))
    f = c.drawRightString if align=='right' else (c.drawCentredString if align=='center' else c.drawString)
    f(x,H-y,s)

def wrap(s,width,size=16,bold=False):
    font = 'Helvetica-Bold' if bold else 'Helvetica'
    lines=[]
    for p in s.split('\n'):
        line=''
        for word in p.split():
            candidate=(line+' '+word).strip()
            if stringWidth(candidate,font,size)>width and line:
                lines.append(line); line=word
            else: line=candidate
        lines.append(line)
    return lines

def para(x,y,s,width,size=16,leading=22,color='muted',bold=False):
    lines=wrap(s,width,size,bold)
    for i,line in enumerate(lines): txt(x,y+i*leading,line,size,color,bold)
    return y+len(lines)*leading

def arrow(points,color='muted',width=1.8,dash=False,head=True):
    c.setStrokeColor(HexColor(C[color])); c.setFillColor(HexColor(C[color])); c.setLineWidth(width)
    c.setDash(5,4) if dash else c.setDash()
    p=c.beginPath(); p.moveTo(points[0][0],H-points[0][1])
    for x,y in points[1:]: p.lineTo(x,H-y)
    c.drawPath(p)
    c.setDash()
    if head:
        a,b=points[-2],points[-1]
        angle=atan2(b[1]-a[1],b[0]-a[0]); length=8
        p=c.beginPath(); p.moveTo(b[0],H-b[1])
        for off in (-pi/6,pi/6):
            x=b[0]-length*cos(angle+off); y=b[1]-length*sin(angle+off)
            p.lineTo(x,H-y)
        p.close(); c.drawPath(p,fill=1,stroke=0)

def card(x,y,w,h,title,body,fill='white',accent='blue',size=16,title_size=19):
    rect(x,y,w,h,fill)
    c.setFillColor(HexColor(C[accent])); c.roundRect(x+14,H-y-h+14,4,h-28,2,fill=1,stroke=0)
    txt(x+32,y+32,title,title_size,accent,True)
    end=para(x+32,y+59,body,w-52,size,21)
    if end-21 > y+h-12: raise ValueError(f'Card overflow: {title}: {end} > {y+h}')

def small(x,y,w,title,body,tag,accent='blue',fill='white'):
    rect(x,y,w,76,fill,radius=9)
    txt(x+15,y+25,title,17,'ink',True)
    txt(x+w-15,y+24,tag,10,accent,True,'right')
    para(x+15,y+48,body,w-30,14,18)

def header(title,subtitle,page):
    c.setFillColor(HexColor(C['white'])); c.rect(0,0,W,H,fill=1,stroke=0)
    txt(60,32,'HID HACKATHON  /  PROPOSED DESIGN',12,'blue',True)
    txt(60,79,title,38,'ink',True)
    txt(60,111,subtitle,17,'muted')
    txt(1540,32,f'MENTOR REVIEW  |  {page} / 2',12,'muted',True,'right')

def footer(note):
    txt(60,1205,note,11,'muted')
    txt(1540,1205,'14 NOV 2026',11,'muted',False,'right')

# Page 1: mentor-facing, end-to-end diagram.
header('Can You Spot a Spoof?', 'A fast first pass, followed by targeted verification only when evidence is insufficient.',1)
card(60,145,420,132,'0  READY', 'Camera on; consent notice and consent path.\nLoad enrolled identities.\nEmpty-view reference: optional experiment.',size=15)
card(530,145,420,132,'1  ARRIVAL + CLOCK', 'Face enters the interaction zone: start timer.\nTrack one face throughout the attempt.\nTracking loss does not reset the timer.',size=15)
card(1000,145,540,132,'2  STREAM + SELECT', 'Aim for 6-10 spaced, usable frames in about 1 s.\nKeep quality / continuity statistics across the stream.\nSend the first good identity frame immediately.',size=15)
arrow([(480,211),(530,211)])
arrow([(950,211),(1000,211)])
arrow([(1270,277),(1270,314)])

rect(60,314,1480,236,'pale',radius=16)
txt(82,344,'3  PARALLEL CHECKS',18,'blue',True)
txt(350,344,'Process as frames arrive - capture and analysis overlap.',16,'muted')
small(80,366,460,'Identity match','Compare the visitor with the enrolled identity.','AWS')
small(570,366,460,'Anti-spoof model','Assess liveness across several usable frames.','LOCAL CORE')
small(1060,366,460,'Quality + continuity','Check visibility, usable detail and the same face.','LOCAL CORE')
small(80,456,460,'Object / face overlap','Localized phone or paper cues; not generic labels.','AWS SUPPORT')
small(570,456,460,'Facial motion','Relative movement within the face, not motion alone.','LOCAL SUPPORT')
small(1060,456,460,'Scene context','Background and boundaries: supporting clues only.','OPTIONAL',accent='purple')

card(60,587,370,107,'EXPERIMENTS ONLY','Screen illumination / greeting gaze.\nAdd only if tests show useful evidence.',fill='purplebg',accent='purple',size=14,title_size=16)
arrow([(430,641),(470,641)],'purple',dash=True)
rect(470,590,660,104,'ink','ink')
txt(800,624,'4  EVIDENCE + DEADLINE',22,'white',True,'center')
txt(800,651,'Require identity match + positive, validated liveness evidence.',16,'white',False,'center')
txt(800,675,'Missing or conflicting evidence goes to verification.',16,'white',False,'center')
arrow([(800,550),(800,590)])
card(1170,587,370,107,'TIMING TARGET','From arrival: decide <= 1.7 s; display <= 2 s.\nStep-up duration is measured separately.',fill='graybg',accent='blue',size=14,title_size=16)

arrow([(800,694),(800,730)],head=False)
arrow([(300,730),(1300,730)],head=False)
for x in (300,800,1300): arrow([(x,730),(x,765)])
card(60,765,460,115,'ALLOW','Identity match + adequate capture quality.\nStrong liveness evidence; no unresolved conflict.',fill='greenbg',accent='green',size=15,title_size=24)
card(570,765,460,115,'REQUEST VERIFICATION','Weak, missing or conflicting evidence.\nPoor quality, tracking loss or timeout.',fill='amberbg',accent='amber',size=15,title_size=21)
card(1080,765,460,115,'DENY','Clear spoof evidence, or a clear identity\nmismatch on a usable capture.',fill='redbg',accent='red',size=15,title_size=24)
arrow([(800,880),(800,922)],'amber')

card(60,922,460,153,'MASK VALIDATION GATE','If a worn paper mask receives ALLOW:\ndisable passive approvals, require active\nverification for all, and strengthen / retest it.\nA smile alone is not proof against every mask.',fill='graybg',accent='blue',size=15,title_size=18)
card(570,922,460,153,'5  TARGETED VERIFICATION','Quality issue -> guided recapture.\nLiveness doubt -> random expression + head turn.\nRecheck -> ALLOW or DENY.\nStill unsure -> reception; no automatic access.',fill='amberbg',accent='amber',size=15,title_size=18)
card(1080,922,460,153,'NO AUTOMATIC BYPASS','Detected spoof -> DENY; no OTP bypass.\nOTP proves phone possession, not liveness.\nManual clearance is a separate decision.',fill='graybg',accent='red',size=15,title_size=18)

arrow([(300,880),(300,900),(40,900),(40,1100),(300,1100),(300,1120)],'green')
arrow([(1300,880),(1300,900),(1560,900),(1560,1100),(1300,1100),(1300,1120)],'red')
arrow([(800,1075),(800,1120)],'amber')
rect(60,1120,1480,66,'ink','ink',12)
txt(82,1150,'6  CLOSE ATTEMPT',18,'white',True)
txt(315,1147,'Show result + fixed reason  |  Log outcome, signals and timings  |  Clean up transient frames',16,'white')
txt(315,1170,'Optional Bedrock explanation follows the verdict. Enrolled references are retained separately.',14,'white')
footer('Targets and attack coverage are unmeasured. Confirm whether the 2 s limit permits a longer verification step.')
c.showPage()

# Page 2: keep the alternative attachment architecture explicit, not silently merged.
header('Implementation options & validation', 'The posted flow and the attached local design are alternatives. Choose one implementation before integration.',2)
rect(60,150,1480,86,'ink','ink')
txt(800,184,'ONE SHARED USER EXPERIENCE',17,'white',True,'center')
txt(800,214,'Capture  ->  Parallel evidence  ->  ALLOW / REQUEST / DENY  ->  Targeted verification if needed',21,'white',False,'center')

rect(60,270,710,563,'pale',radius=16)
rect(830,270,710,563,'graybg',radius=16)
txt(84,305,'A  POSTED AWS-ASSISTED FLOW',22,'blue',True)
txt(854,305,'B  FRIEND\'S LOCAL DESIGN',22,'blue',True)

card(100,330,630,85,'Browser capture + tracking','Stream frames; select usable samples; keep the attempt clock.',size=15,title_size=18)
arrow([(415,415),(415,440)],head=False)
arrow([(250,440),(590,440)],head=False)
arrow([(250,440),(250,465)])
arrow([(590,440),(590,465)])
card(85,465,325,125,'Local liveness','Pretrained anti-spoof model.\nQuality + face continuity.\nMotion as supporting evidence.',size=15,title_size=18)
card(440,465,305,125,'AWS services','CompareFaces: identity.\nDetectLabels: object cues.\nCalls overlap local work.',size=15,title_size=18)
arrow([(250,590),(250,620)],head=False)
arrow([(590,590),(590,620)],head=False)
arrow([(250,620),(590,620)],head=False)
arrow([(415,620),(415,650)])
card(100,650,630,90,'Fuse evidence + enforce deadline','All required evidence must pass; uncertainty remains explicit.',fill='white',size=15,title_size=18)
para(88,778,'Model deployment remains a choice: browser inference or a local Python service. No new trained SVM is required for this option.',650,15,21)

card(870,330,630,85,'Python webcam stream + YuNet','Bounded queue; one tracked face; about 1 s split into 6 slots.',size=15,title_size=18)
arrow([(1185,415),(1185,440)],head=False)
arrow([(1015,440),(1360,440)],head=False)
arrow([(1015,440),(1015,465)])
arrow([(1360,440),(1360,465)])
card(850,465,330,105,'Distortion + FFT features','Include temporal statistics.\nShared training / inference extraction.',size=14,title_size=18)
card(1210,465,305,125,'SFace identity','Embed a selected face frame.\nCompare with enrolled embeddings.\nTest the match threshold locally.',size=14,title_size=18)
arrow([(1015,570),(1015,600)])
card(850,600,330,100,'Trained SVM','Grouped validation + score calibration.\nConservative burst aggregation.',size=14,title_size=18)
arrow([(1015,700),(1015,717)],head=False)
arrow([(1360,590),(1360,717)],head=False)
arrow([(1015,717),(1360,717)],head=False)
arrow([(1185,717),(1185,735)])
rect(870,735,630,75,'white')
txt(900,765,'Same three-outcome policy',18,'blue',True)
txt(900,791,'Requires collected data, training and independent validation.',15,'muted')

card(60,866,710,145,'BEFORE ENABLING PASSIVE ALLOW','Test genuine, impostor, print, screen-photo, video and worn masks.\nKeep threshold-setting attempts separate from reported tests.\nNo motion or no visible object is not positive proof of liveness.\nIf masks pass, strengthen verification and retest before approving.',fill='pale',size=15,title_size=18)
card(830,866,710,145,'FALLBACK MUST BE IMPLEMENTED TOO','Use an expression / dense-landmark detector for the active action.\nRequire the response to start after the prompt; track the same face.\nContinue anti-spoof checks; an action never clears a detected spoof.\nUnresolved cases stay unapproved or go to staffed verification.',fill='amberbg',accent='amber',size=15,title_size=18)

rect(60,1045,1480,141,'ink','ink')
txt(85,1076,'MEASURE WHAT THE MENTORS WILL SEE',19,'white',True)
txt(85,1106,'Per scenario: ALLOW / REQUEST / DENY counts, attack acceptance, genuine rejection and challenge rate.',17,'white')
txt(85,1135,'Timing: arrival-to-visible-result median + p95; first pass and full verification reported separately.',17,'white')
txt(85,1164,'Cleanup: remove transient captures after processing; retain enrollment references and image-free metrics separately.',16,'white')
footer('Both paths are design proposals from the supplied notes; implementation and measured performance were not verified here.')
c.save()
print(PDF)
