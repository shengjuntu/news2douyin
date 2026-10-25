(()=>{
 const id=window.shortScriptCase,box=document.getElementById('short-list'),message=document.getElementById('short-message');
 const names={not_started:'尚未启动',queued:'排队中',preparing:'准备环境',starting:'启动中',running:'补材与写稿中',waiting:'等待处理',stopping:'正在停止',uncertain:'正在核对执行状态',completed:'执行已结束',partial:'部分完成',failed:'执行失败',paused:'已停止'};
 const languages={'zh-CN':'简体中文',en:'English',ja:'日本語'};
 const active=new Set(['queued','preparing','starting','running','waiting','stopping','uncertain']);
 let timer,busy=false,loading=false;
 async function api(url,body){const r=await fetch(url,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));return d;}
 function element(tag,text){const e=document.createElement(tag);if(text)e.textContent=text;return e;}
 function link(text,url){const e=element('a',text);e.href=url;return e;}
 async function action(url){if(busy)return;busy=true;try{await api(url,{});message.textContent='请求已保存';}catch(e){message.textContent=e.message;}finally{busy=false;await load();}}
 function render(rows){box.replaceChildren();if(!rows.length){box.append(element('p','还没有补材任务。点击上方“选择新闻，开始补材”，选好报道后确认启动。'));return;}
  for(const row of rows){const card=element('article');card.className='card';card.style.marginBottom='16px';card.append(element('h2',row.title));card.append(element('p',(row.package_key?'短稿已保存 · 待审核':names[row.status]||row.status)+' · '+languages[row.options.output_language]+' · '+row.options.duration_sec+' 秒'));card.append(element('p','外部搜索 '+row.search_count+'/3 · 新增正文读取 '+row.read_count+'/6 · 配图 '+(row.options.find_images?'开启':'关闭')));
   if(row.error)card.append(element('p',row.error));
   if(!active.has(row.status)&&!row.package_key)card.append(element('p','本次尚未生成可用短稿。查看证据与缺口后可继续补材。'));
   const controls=element('div');controls.className='row';controls.append(link('查看原文、证据和执行进度','/research/'+encodeURIComponent(row.case_id)));if(!id)controls.append(link('打开任务','/short-scripts/'+encodeURIComponent(row.case_id)));
   if(row.script_url)controls.append(link('打开短稿与配图',row.script_url));
   if(active.has(row.status)&&row.status!=='stopping'){const stop=element('button','停止');stop.className='ghost';stop.disabled=busy;stop.onclick=()=>action('/api/research/cases/'+encodeURIComponent(row.case_id)+'/stop');controls.append(stop);}
   if(!active.has(row.status)&&!row.package_key){const start=element('button','继续补材并生成');start.disabled=busy;start.onclick=()=>action('/api/short-scripts/'+encodeURIComponent(row.case_id)+'/start');controls.append(start);}
   card.append(controls);box.append(card);
  }
 }
 async function load(){if(loading)return;loading=true;try{const data=await api('/api/short-scripts'+(id?'/'+encodeURIComponent(id):''));render(id?[data]:data);}catch(e){message.textContent=e.message;}finally{loading=false;clearTimeout(timer);timer=setTimeout(load,4000);}}
 document.addEventListener('visibilitychange',()=>{if(document.hidden)clearTimeout(timer);else load();});window.addEventListener('pagehide',()=>clearTimeout(timer));load();
})();
