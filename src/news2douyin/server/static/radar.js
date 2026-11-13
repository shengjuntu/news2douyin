(()=>{'use strict';
const root=document.querySelector('[data-radar]');if(!root)return;
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
const labels={meeting:'会谈',visit:'访问',signing:'签署',attack:'袭击'};
const palette=['#62d0d0','#b599ee','#e8b26c','#7caee9','#91c598','#e389a8','#cfcd7c','#88b8bc'];
let payload=null,promise=null,network=null,country=null,demo=false;
const networkHost=document.querySelector('[data-radar-network]'),countryHost=document.querySelector('[data-radar-countries]');
// Counts run independently from first-paint records. A view switch shares the
// same in-flight promise; refresh explicitly asks the server for current data.
const facetRequests=new Map(),facetStatus=document.querySelector('[data-radar-facet-status]'),facetRetry=document.querySelector('[data-radar-facet-retry]');
function facetKey(){const u=new URL(root.dataset.radarFacetUrl,location.href);for(const key of ['offset','limit','view'])u.searchParams.delete(key);u.searchParams.sort();return u.pathname+'?'+u.searchParams;}
function applyFacets(d){
 const valid=n=>typeof n==='number'&&Number.isSafeInteger(n)&&n>=0;
 if(!valid(d.total)||!d.radar_stats||!['total','analyzed','source_count'].every(k=>valid(d.radar_stats[k]))||!Array.isArray(d.channels)||!Array.isArray(d.providers)||![...d.channels,...d.providers].every(row=>valid(row.count)))throw Error('精确统计响应不完整');
 for(const n of root.querySelectorAll('[data-radar-stat]'))n.textContent=(n.dataset.radarStat==='total'?d.total:d.radar_stats[n.dataset.radarStat]).toLocaleString('zh-CN');
 const channels=new Map(d.channels.map(row=>[row.id,row.count])),providers=new Map(d.providers.map(row=>[row.id,row.count]));
 for(const n of root.querySelectorAll('[data-radar-channel-count]'))n.textContent=channels.has(n.dataset.radarChannelCount)?channels.get(n.dataset.radarChannelCount).toLocaleString('zh-CN'):'…';
 for(const n of root.querySelectorAll('[data-radar-provider-label]'))n.textContent=n.dataset.radarProviderLabel+' · '+(providers.has(n.value)?providers.get(n.value).toLocaleString('zh-CN'):'…');
 const history=root.querySelector('[data-radar-historical]');if(history)history.hidden=!(d.historical&&d.total>0);
 const empty=root.querySelector('[data-radar-empty-label]');if(empty)empty.textContent=d.total===0?'当前范围没有事件。':'当前页没有可展示的事件，可返回上一页查看。';
}
async function loadFacets(refresh=false){
 if(!root.dataset.radarFacetUrl||!facetStatus)return;
 const key=facetKey();if(refresh)facetRequests.delete(key);
 for(const n of root.querySelectorAll('[data-radar-stat],[data-radar-channel-count]'))n.textContent='…';
 for(const n of root.querySelectorAll('[data-radar-provider-label]'))n.textContent=n.dataset.radarProviderLabel+' · …';
 const empty=root.querySelector('[data-radar-empty-label]');if(empty)empty.textContent='当前页没有可展示的事件，完整范围统计中。';
 root.dataset.facetState='loading';facetStatus.classList.remove('desk-error');facetStatus.textContent='精确统计中，事件列表可继续操作。';facetRetry.disabled=true;facetRetry.textContent='统计中…';
 if(!facetRequests.has(key))facetRequests.set(key,fetch(key,{cache:'no-store'}).then(async response=>{if(!response.ok)throw Error('统计服务暂时不可用');const result=await response.json();return result;}));
 try{const result=await facetRequests.get(key);if(key!==facetKey())return;applyFacets(result);root.dataset.facetState='ready';facetStatus.textContent='当前范围的精确统计已更新。';facetRetry.textContent='刷新统计';}
 catch(error){facetRequests.delete(key);if(key!==facetKey())return;root.dataset.facetState='error';facetStatus.classList.add('desk-error');facetStatus.textContent='精确统计暂未完成，请重试。事件列表和翻页仍可使用。';facetRetry.textContent='重试统计';}
 finally{if(key===facetKey())facetRetry.disabled=false;}
}
facetRetry?.addEventListener('click',()=>loadFacets(true));
function demoData(){const conflict=new URL(location.href).searchParams.get('channel')==='conflict';return {nodes:[{id:'demo-a',label:'甲国'},{id:'demo-b',label:'乙国'},{id:'demo-c',label:'丙国'},{id:'demo-d',label:'丁国'}],links:(conflict?[[0,1,4],[1,2,2],[2,0,1],[3,1,3]]:[[0,1,4],[1,2,3],[2,3,2],[3,0,1],[0,2,2]]).map(([a,b,value])=>({source:'demo-'+String.fromCharCode(97+a),target:'demo-'+String.fromCharCode(97+b),value,directed:conflict||a%2===0,kind:conflict?'attack':'meeting',evidence:[],demo:true})),eligible_mentions:0,excluded_mentions:0,measure:'虚构关系权重',demo:true};}
async function data(){if(payload)return payload;if(!promise){const params=new URLSearchParams(location.search);params.delete('offset');promise=fetch('/api/radar/view?'+params).then(async r=>{if(!r.ok)throw Error('视图数据暂时无法读取');payload=await r.json();const note=document.querySelector('[data-radar-scope]');if(payload.scope_note)note.textContent=payload.scope_note;return payload;}).catch(e=>{promise=null;throw e;});}return promise;}
function countryMount(host,data){
 host.replaceChildren();let zoom,svg,observer,dialog,marker,toolbarMarker;
 const toolbar=el('div',undefined,'ng-toolbar');const buttons={};for(const [id,label]of [['zoom-in','放大 +'],['zoom-out','缩小 −'],['fit','居中'],['expand','放大查看']]){const b=el('button',label,'ghost');b.type='button';b.dataset.countryAction=id;buttons[id]=b;toolbar.append(b);}host.append(toolbar);
 const coverage=el('div',undefined,'country-coverage');if(!data.demo){coverage.append(el('span','可绘图陈述 '+(data.eligible_mentions||0)),el('span','未纳入 '+(data.excluded_mentions||0)),el('span',(data.nodes||[]).length+' 个国家'));if(data.truncated)coverage.append(el('span','已达国家或关系显示上限；详细事件仍可在列表查看。'));if(data.mentions_truncated)coverage.append(el('span','已达 '+data.mention_limit+' 条陈述样本上限。'));}host.append(coverage);
 const main=el('div',undefined,'country-main'),canvas=el('div',undefined,'country-canvas'),aside=el('aside',undefined,'country-selection');canvas.dataset.countryChart='';main.append(canvas,aside);host.append(main);
 const legend=el('p',data.demo?'示意仅用于体验交互；颜色表示国家，弦宽是虚构权重。':'箭头表示原文中明确的行动者 → 对象；没有箭头的弦表示共同参与。弦宽为不同事件记录数，同一事件可涉及多条关系。','ng-legend');host.append(legend);
 const accessible=el('details',undefined,'ng-list');accessible.append(el('summary','国家关系与证据的文字列表'));const rows=el('ul');accessible.append(rows);host.append(accessible);
 const nodes=data.nodes||[],byId=new Map(nodes.map(n=>[n.id,n])),links=(data.links||[]).filter(e=>byId.has(e.source)&&byId.has(e.target));
 function show(records,heading){aside.replaceChildren(el('h3',heading));if(data.demo){aside.append(el('p','虚构示意数据，不代表实际外交或冲突。这里没有新闻证据，不能形成实际选题。'));return;}
  for(const r of records){const p=el('div',undefined,'country-proof');p.append(el('strong',byId.get(r.source).label+(r.directed?' → ':' ↔ ')+byId.get(r.target).label),el('p',(labels[r.kind]||r.kind)+' · '+r.value+' 个事件记录 · '+(r.directed?'有方向的角色关系':'共同参与，无方向')));for(const proof of r.evidence||[]){const box=el('div',undefined,'country-proof');const q=el('blockquote',proof.quote||'查看原文中的角色依据');box.append(q);if(proof.roles?.length)box.append(el('p',proof.roles.map(a=>({actor:'行动者',target:'对象',participant:'参与方'}[a.role]||a.role)+'：'+a.text+(a.country?'（'+a.country+'）':'')).join(' · '),'country-roles'));if(proof.date)box.append(el('p','文中行动日期 '+proof.date));const line=el('div',undefined,'row');if(proof.event){const a=el('a','查看事件');a.href='/events/'+encodeURIComponent(proof.event);line.append(a);}if(proof.article){const a=el('a','原文 r'+(proof.revision||'?'));a.href='/articles/'+encodeURIComponent(proof.article)+(proof.revision?'?revision='+encodeURIComponent(proof.revision):'');line.append(a);}box.append(line);p.append(box);}if(r.evidence_truncated)p.append(el('p','仅列出部分证据，完整来源请进入事件。'));aside.append(p);}
  if(!records.length)aside.append(el('p','当前国家没有可展示的双边关系。'));
 }
 aside.append(el('h3','国家之间的行动'),el('p','点国家查看相关关系，点弦核对国家角色和原文。拖动图面或滚轮缩放。'));
 if(!data.demo){aside.append(el('p','仅纳入有明确国家主体 / 对象的已报道行动。人物国籍、行动地点和媒体来源国不会自动变成国家关系。'));if(data.excluded_reasons){const detail=el('details');detail.append(el('summary','为什么有些材料没有画入？'));for(const [reason,count]of Object.entries(data.excluded_reasons))detail.append(el('p',({not_reported:'计划、否认或未确定发生的陈述',no_explicit_country_pair:'尚无明确的国家行动角色配对'}[reason]||reason)+'：'+count));aside.append(detail);}}
 for(const r of links){const li=el('li'),b=el('button',byId.get(r.source).label+(r.directed?' → ':' ↔ ')+byId.get(r.target).label+' · '+(labels[r.kind]||r.kind)+' · '+r.value);b.type='button';b.dataset.countryRelation=r.source+':'+r.target;b.dataset.countryKind=r.kind;b.dataset.countryDirected=String(!!r.directed);b.onclick=()=>show([r],'关系与原文');li.append(b);rows.append(li);}
 if(!nodes.length||!links.length){canvas.append(el('div','现有材料尚不足以绘制国家关系。\n需要同一条已报道行动中明确出现两个国家的参与角色；地点提及和媒体来源国不会用于补线。','country-empty'));for(const b of Object.values(buttons))b.disabled=true;return {destroy(){}};}
 if(!window.d3){canvas.append(el('div','圆形图组件未能加载。下方文字列表仍可查看关系和证据。','country-empty'));for(const b of Object.values(buttons))b.disabled=true;return {destroy(){}};}
 const d3=window.d3;
 // Each country has private slots for kind + direction. Its outer arc stays
 // unified, but every relation retains an independently selectable ribbon.
 const slots=[],slotKey=(country,kind,directed)=>country+'|'+kind+'|'+directed;
 for(const [countryIndex,n] of nodes.entries()){
  const types=[...new Map(links.filter(r=>r.source===n.id||r.target===n.id).map(r=>[r.kind+'|'+r.directed,{kind:r.kind,directed:r.directed}])).values()].sort((a,b)=>a.kind.localeCompare(b.kind)||Number(a.directed)-Number(b.directed));
  for(const type of types)slots.push({countryIndex,id:n.id,...type});
 }
 const slotIndex=new Map(slots.map((s,i)=>[slotKey(s.id,s.kind,s.directed),i]));
 const matrix=slots.map(()=>slots.map(()=>0)),records=new Map();
 for(const r of links){const a=slotIndex.get(slotKey(r.source,r.kind,r.directed)),b=slotIndex.get(slotKey(r.target,r.kind,r.directed));matrix[a][b]+=Number(r.value)||0;const key=a+':'+b;if(!records.has(key))records.set(key,[]);records.get(key).push(r);}
 const chords=d3.chordDirected().padAngle(Math.min(.028,2*Math.PI/slots.length*.12)).sortSubgroups(d3.descending)(matrix),radius=240;
 const countryGroups=nodes.map((n,index)=>{const parts=chords.groups.filter(g=>slots[g.index].countryIndex===index);return {index,startAngle:Math.min(...parts.map(g=>g.startAngle)),endAngle:Math.max(...parts.map(g=>g.endAngle)),value:parts.reduce((sum,g)=>sum+g.value,0)};}).filter(g=>Number.isFinite(g.startAngle));
 svg=d3.select(canvas).append('svg').attr('viewBox','0 0 960 720').attr('role','img').attr('aria-label',data.demo?'虚构示意国家圆形关系图':'国家行动圆形关系图');
 const world=svg.append('g'),shape=world.append('g').attr('transform','translate(480,360)');
 const zoomLabel=el('span','100%','country-zoom muted small');zoomLabel.setAttribute('aria-label','国家图缩放比例');toolbar.append(zoomLabel);
 zoom=d3.zoom().extent([[0,0],[960,720]]).scaleExtent([.45,5]).on('zoom',e=>{world.attr('transform',e.transform);zoomLabel.textContent=Math.round(e.transform.k*100)+'%';});svg.call(zoom);
 const ribbonArrow=d3.ribbonArrow().radius(radius-7).padAngle(.007),ribbon=d3.ribbon().radius(radius-7).padAngle(.007);
 const proofs=d=>records.get(d.source.index+':'+d.target.index)||[],first=d=>proofs(d)[0],countryIndex=d=>slots[d.source.index].countryIndex;
 const ribbons=shape.append('g').selectAll('path').data(chords).join('path').attr('d',d=>first(d)?.directed?ribbonArrow(d):ribbon(d)).attr('fill',d=>palette[countryIndex(d)%palette.length]).attr('fill-opacity',.58).attr('stroke',d=>palette[countryIndex(d)%palette.length]).attr('stroke-opacity',.7).attr('tabindex',0).attr('role','button').attr('data-country-kind',d=>first(d)?.kind).attr('data-country-directed',d=>String(!!first(d)?.directed)).attr('aria-label',d=>{const r=first(d);return byId.get(r.source).label+(r.directed?'行动者到对象':'共同参与')+byId.get(r.target).label+'，'+(labels[r.kind]||r.kind)+'，'+r.value+'个事件记录';}).on('mouseenter',function(){d3.select(this).attr('fill-opacity',.95);}).on('mouseleave',function(){d3.select(this).attr('fill-opacity',.58);}).on('click',(e,d)=>show(proofs(d),'关系与原文')).on('keydown',(e,d)=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();show(proofs(d),'关系与原文');}});
 ribbons.append('title').text(d=>{const r=first(d);return byId.get(r.source).label+(r.directed?' → ':' ↔ ')+byId.get(r.target).label+' · '+(labels[r.kind]||r.kind)+' · '+r.value+'个事件记录，点击查看证据';});
 const arc=d3.arc().innerRadius(radius).outerRadius(radius+18),groups=shape.append('g').selectAll('g').data(countryGroups).join('g');
 const highlight=i=>ribbons.attr('opacity',d=>i===null||slots[d.source.index].countryIndex===i||slots[d.target.index].countryIndex===i?1:.12);
 groups.append('path').attr('d',arc).attr('fill',d=>palette[d.index%palette.length]).attr('stroke','#b5dce4').attr('tabindex',0).attr('role','button').attr('aria-label',d=>nodes[d.index].label+'的所有关系').on('mouseenter',(e,d)=>highlight(d.index)).on('mouseleave',()=>highlight(null)).on('click',(e,d)=>show(links.filter(r=>r.source===nodes[d.index].id||r.target===nodes[d.index].id),nodes[d.index].label+'的关系')).on('keydown',(e,d)=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();show(links.filter(r=>r.source===nodes[d.index].id||r.target===nodes[d.index].id),nodes[d.index].label+'的关系');}});
 groups.append('text').each(d=>d.angle=(d.startAngle+d.endAngle)/2).attr('transform',d=>'rotate('+(d.angle*180/Math.PI-90)+') translate('+(radius+31)+')'+(d.angle>Math.PI?' rotate(180)':'')).attr('dy','.35em').attr('text-anchor',d=>d.angle>Math.PI?'end':'start').text(d=>nodes[d.index].label+' '+nodes[d.index].id);
 buttons['zoom-in'].onclick=()=>svg.transition().duration(160).call(zoom.scaleBy,1.25,[480,360]);buttons['zoom-out'].onclick=()=>svg.transition().duration(160).call(zoom.scaleBy,.8,[480,360]);buttons.fit.onclick=()=>svg.transition().duration(160).call(zoom.transform,d3.zoomIdentity);
 dialog=el('dialog',undefined,'country-dialog');dialog.dataset.radarDialog='';dialog.setAttribute('aria-label','放大国家关系图');const head=el('div',undefined,'ng-dialog-heading'),close=el('button','关闭','ghost');close.type='button';head.append(el('h2','国家圆形关系图'+(data.demo?' · 虚构示意':'')),close);dialog.append(head);document.body.append(dialog);
 buttons.expand.onclick=()=>{if(dialog.open)return;buttons.expand.disabled=true;marker=el('span');toolbarMarker=el('span');main.before(marker);toolbar.before(toolbarMarker);dialog.append(toolbar,main);dialog.showModal();};close.onclick=()=>dialog.close();dialog.addEventListener('close',()=>{buttons.expand.disabled=false;if(marker){marker.replaceWith(main);toolbarMarker.replaceWith(toolbar);marker=null;toolbarMarker=null;buttons.expand.focus();}});
 return {destroy(){if(dialog.open)dialog.close();dialog.remove();observer?.disconnect();}};
}
async function render(view){const status=document.querySelector('[data-radar-load]');if(view==='list'){status.textContent='';return;}
 status.textContent='正在读取当前范围的图数据…';
 try{const d=await data();if(view==='network'&&!network){network=window.N2DGraph.mount(networkHost,d.network||{nodes:[],edges:[]});}
  if(view==='countries'){country?.destroy();country=countryMount(countryHost,demo?demoData():d.countries||{nodes:[],links:[]});}
  status.textContent=(d.truncated?'当前范围较大，图基于前 '+d.sampled+' 个事件；列表保留完整分页。':'当前筛选 '+d.total+' 个事件。')+(view==='network'&&d.network?.truncated?' 网络图节点已截取。':'');
 }catch(e){status.textContent=e.message;status.classList.add('desk-error');}
}
function activate(view,push){if(!['list','network','countries'].includes(view))view='list';root.dataset.currentView=view;for(const panel of document.querySelectorAll('[data-radar-panel]'))panel.hidden=panel.dataset.radarPanel!==view;for(const a of document.querySelectorAll('[data-radar-view]')){const on=a.dataset.radarView===view;a.classList.toggle('on',on);if(on)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');}const input=document.querySelector('#radar-view-input');if(input)input.value=view;for(const a of document.querySelectorAll('.radar-channels a,.radar-pagination a')){const u=new URL(a.href,location.href);u.searchParams.set('view',view);a.href=u.pathname+u.search;}if(push){const u=new URL(location.href);u.searchParams.set('view',view);history.pushState({},'',u);}render(view);}
for(const a of document.querySelectorAll('[data-radar-view]'))a.addEventListener('click',e=>{if(e.ctrlKey||e.metaKey||e.shiftKey||e.altKey)return;e.preventDefault();activate(a.dataset.radarView,true);});
const demoButton=document.querySelector('[data-radar-demo]'),banner=document.querySelector('[data-radar-demo-banner]');demoButton?.addEventListener('click',()=>{demo=!demo;banner.hidden=!demo;demoButton.textContent=demo?'切回真实数据':'查看虚构示意图';activate('countries',false);});
window.addEventListener('popstate',()=>activate(new URL(location.href).searchParams.get('view')||'list',false));
activate(new URL(location.href).searchParams.get('view')||'list',false);
loadFacets();
})();
