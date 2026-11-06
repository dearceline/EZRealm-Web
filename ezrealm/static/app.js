'use strict';
const $ = id => document.getElementById(id);
let csrf = '', revision = '', editing = null, rows = [], busy = false;
function message(text, error = false) { $('message').textContent = text; $('message').className = error ? 'error' : ''; }
function showLogin() { $('login').hidden = false; $('dashboard').hidden = true; $('logout').hidden = true; }
async function api(path, body) {
  const options = body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':csrf}, body:JSON.stringify(body)};
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) { if (response.status === 401) showLogin(); throw new Error(data.error || '请求失败'); }
  return data;
}
async function task(fn) {
  if (busy) return;
  busy = true;
  document.querySelectorAll('button').forEach(x => x.disabled = true);
  try { await fn(); } catch (e) { message(e.message, true); }
  finally { busy = false; document.querySelectorAll('button').forEach(x => x.disabled = false); }
}
function confirmAction(text) {
  $('confirmText').textContent = text;
  $('confirm').showModal();
  // Confirmation stays usable while a background action is marked busy.
  $('confirm').querySelectorAll('button').forEach(x => x.disabled = false);
  return new Promise(resolve => $('confirm').addEventListener('close', () => resolve($('confirm').returnValue === 'yes'), {once:true}));
}
async function refresh() {
  const state = await api('/api/state');
  csrf = state.csrf; revision = state.revision; rows = state.rules;
  $('login').hidden = true; $('dashboard').hidden = false; $('logout').hidden = false;
  $('count').textContent = rows.length;
  $('service').textContent = state.service.active === 'active' ? '运行中' : '已停止';
  $('rules').replaceChildren(); $('empty').hidden = rows.length > 0;
  for (const row of rows) {
    const tr = document.createElement('tr');
    for (const key of ['id','listen','remote','protocol','note']) {
      const td = document.createElement('td');
      if (key === 'protocol') { const span = document.createElement('span'); span.className='badge'; span.textContent=row[key].toUpperCase(); td.append(span); }
      else td.textContent = row[key];
      tr.append(td);
    }
    const actions = document.createElement('td');
    const edit = document.createElement('button'); edit.textContent='修改';
    edit.addEventListener('click', () => { editing=row.id; for (const key of ['listen','remote','protocol','note']) $('ruleForm').elements[key].value=row[key]; $('formTitle').textContent=`修改规则 #${row.id}`; $('cancelEdit').hidden=false; $('ruleForm').elements.listen.focus(); });
    const remove = document.createElement('button'); remove.textContent='删除';
    remove.addEventListener('click', () => task(async () => { if (await confirmAction(`删除规则 #${row.id}：${row.listen} → ${row.remote}？`)) { await api('/api/action',{action:'delete',data:{id:row.id},revision}); resetForm(); await refresh(); message('规则已删除，应用配置后生效。'); } }));
    actions.append(edit, remove); tr.append(actions); $('rules').append(tr);
  }
}
function resetForm() { editing=null; $('ruleForm').reset(); $('formTitle').textContent='添加转发规则'; $('cancelEdit').hidden=true; }
$('loginForm').addEventListener('submit', e => { e.preventDefault(); task(async () => { const f=e.target.elements; const data=await api('/api/login',{username:f.username.value,password:f.password.value}); csrf=data.csrf; f.password.value=''; await refresh(); message('已安全连接。'); }); });
$('logout').addEventListener('click', () => task(async () => { await api('/api/logout',{}); csrf=''; showLogin(); message('已退出登录。'); }));
$('refresh').addEventListener('click', () => task(async () => { resetForm(); await refresh(); message('已刷新。'); }));
$('cancelEdit').addEventListener('click', resetForm);
$('ruleForm').addEventListener('submit', e => { e.preventDefault(); task(async () => { const f=e.target.elements; const data={listen:f.listen.value.trim(),remote:f.remote.value.trim(),protocol:f.protocol.value,note:f.note.value}; if(editing!==null)data.id=editing; await api('/api/action',{action:editing===null?'add':'edit',data,revision}); resetForm(); await refresh(); message('规则已保存并备份，应用配置后生效。'); }); });
$('importForm').addEventListener('submit', e => { e.preventDefault(); task(async () => { const state=await api('/api/action',{action:'import',data:e.target.elements.records.value,revision}); const r=state.report; $('report').textContent=`成功 ${r.success} · 重复 ${r.duplicate} · 失败 ${r.failed}\n`+r.details.map(x=>`第 ${x.line} 行：${x.message}`).join('\n'); resetForm(); await refresh(); message('导入完成，查看下方结果；应用配置后生效。'); }); });
for (const [id, action, text] of [['start','start','启动 Realm 服务并使用当前保存的配置？'],['stop','stop','停止 Realm 服务？现有转发连接将中断。'],['apply','restart','应用已保存配置并重启 Realm？现有转发连接将中断。']]) $(id).addEventListener('click', () => task(async () => { if(await confirmAction(text)){ await api('/api/action',{action:'service',data:action}); await refresh(); message('服务操作完成。'); } }));
for (const [id, path, name] of [['export','/api/export','realm-rules.txt'],['fullExport','/api/export/full','realm-config.txt']]) $(id).addEventListener('click', () => task(async () => { const data=await api(path); const url=URL.createObjectURL(new Blob([data.text],{type:'text/plain;charset=utf-8'})); const a=document.createElement('a'); a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000); message(id==='export'?'已导出四列规则。此格式不包含监听 IP、协议及高级参数；完整迁移请用“完整备份”。':'已导出完整配置（保留全部参数）。'); }));
task(async () => { try { await refresh(); } catch(e) { showLogin(); if(e.message!=='请先登录') throw e; } });
