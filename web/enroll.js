const el = id => document.getElementById(id);
let stream, captured;
function stopCamera() { stream?.getTracks().forEach(t => t.stop()); stream = null; el('capture').disabled = true; }
window.addEventListener('pagehide', stopCamera);
el('camera').onclick = async () => {
  try { stopCamera(); stream = await navigator.mediaDevices.getUserMedia({video:true,audio:false}); el('video').srcObject = stream; await el('video').play(); el('capture').disabled = false; }
  catch (e) { el('message').textContent = 'Camera unavailable: ' + e.message; }
};
el('capture').onclick = () => {
  const v = el('video'), c = document.createElement('canvas');
  if (!v.videoWidth) return;
  c.width = 640; c.height = Math.round(640*v.videoHeight/v.videoWidth);
  c.getContext('2d').drawImage(v,0,0,c.width,c.height);
  c.toBlob(blob => { captured = blob; el('file').value = ''; el('photoStatus').textContent = 'Reference photo captured; camera released'; stopCamera(); }, 'image/jpeg', .9);
};
el('file').onchange = () => { captured = null; stopCamera(); el('photoStatus').textContent = el('file').files[0]?.name || 'No photo selected'; };
async function request(method, body) {
  const r = await fetch('/enrollment/users', {method, headers:{'X-Enrollment-Token':el('key').value.trim()}, body});
  const d = await r.json(); if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'Check the form fields'); return d;
}
el('list').onclick = async () => { try { const d = await request('GET'); el('users').textContent = 'Enrolled IDs: ' + (d.users.join(', ') || 'none'); } catch(e) { el('message').textContent = e.message; } };
el('form').onsubmit = async e => {
  e.preventDefault(); const photo = captured || el('file').files[0];
  if (!photo) { el('message').textContent = 'Capture or upload a reference photo first.'; return; }
  el('submit').disabled = true;
  try { const body = new FormData(el('form')); body.append('photo',photo,'reference.jpg'); body.append('consent',String(el('consent').checked)); body.append('id_checked',String(el('checked').checked)); const d = await request('POST',body); el('message').textContent = 'Enrolled ' + d.user_id + '. ' + d.message + '\nReturn to verification and enter this ID.'; captured = null; el('file').value = ''; el('key').value = ''; stopCamera(); }
  catch(e) { el('message').textContent = e.message; }
  finally { el('submit').disabled = false; }
};
