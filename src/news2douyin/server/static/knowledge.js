(()=>{'use strict';
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
const link=(label,url)=>{const n=el('a',label);n.href=url;return n;};
const eventLink=(id,title)=>link(title||id,'/events/'+encodeURIComponent(id));
const palette=['#59c8d3','#e8b56c','#9c91ee','#79c7a3','#e38da3','#76a9e7','#cfbe76','#a5c890'];
function mount(host,data){
 host.replaceChildren();const nodes=data.nodes||[],edges=data.edges||[];
 const toolbar=el('div',undefined,'ng-toolbar');host.append(toolbar);
 const actions=[['zoom-in','放大 +'],['zoom-out','缩小 −'],['fit','居中'],['relayout','关系布局'],['circle','圆形布局'],['expand','放大查看']];
 const buttons={};for(const [id,label]of actions){const b=el('button',label,'ghost');b.type='button';b.dataset.ngAction=id;buttons[id]=b;toolbar.append(b);}
 const zoomLabel=el('span','100%','ng-zoom muted small');zoomLabel.setAttribute('aria-label','事件图缩放比例');toolbar.append(zoomLabel);
 const search=el('input');search.type='search';search.placeholder='定位事件';search.setAttribute('aria-label','在关系图中定位事件');toolbar.append(search);
 const main=el('div',undefined,'ng-main'),canvas=el('div',undefined,'ng-canvas'),aside=el('aside',undefined,'ng-selection');canvas.dataset.networkCanvas='';canvas.setAttribute('aria-label','可拖拽缩放的事件关系图');canvas.tabIndex=0;main.append(canvas,aside);host.append(main);
 const legend=el('p','拖拽节点或空白处 · 滚轮 / 双指缩放 · 点事件或连线查看依据。颜色表示关联群组，节点大小表示雷达分数。','ng-legend');host.append(legend);
 const accessible=el('details',undefined,'ng-list');accessible.append(el('summary','事件与关系的文字列表'));const list=el('ul');accessible.append(list);host.append(accessible);
 let cy=null,firstFit=false,resize;
 function defaultAside(){aside.replaceChildren(el('h3','探索事件关系'),el('p',nodes.length+' 个事件 · '+edges.length+' 条关联'),el('p','点选事件查看完整标题、排序依据和采集来源；点选连线查看它为什么建立。关联不代表因果，也不代表同一事件。'));}
 function focusNode(id){const n=nodes.find(n=>n.id===id);if(!n)return;aside.replaceChildren(el('h3',n.title),el('p',n.reason||'尚无图分析分数'));if(n.sources?.length){const sources=el('p');sources.append(el('strong','采集来源：'));sources.append(document.createTextNode(n.sources.map(x=>x.label+(x.count?' × '+x.count:'')).join(' · ')));aside.append(sources);}else aside.append(el('p','采集来源请在事件中的报道详情查看。'));const controls=el('div',undefined,'row');controls.append(eventLink(n.id,'打开事件与原文'),link('形成选题','/workbench/new?event='+encodeURIComponent(n.id)));aside.append(controls);
  if(cy){const target=cy.getElementById(id);cy.elements().removeClass('faded selected');const neighborhood=target.closedNeighborhood();cy.elements().not(neighborhood).addClass('faded');target.addClass('selected');cy.animate({center:{eles:target},duration:200});}
 }
 function focusEdge(e){const a=nodes.find(n=>n.id===e.source),b=nodes.find(n=>n.id===e.target);aside.replaceChildren(el('h3','关联依据'));aside.append(eventLink(e.source,a?.title||e.source),el('p','↔'),eventLink(e.target,b?.title||e.target));for(const proof of e.evidence||[]){const p=el('div',undefined,'ng-edge-evidence');const labels={shared_participant:'共享有出处的行动参与者',same_action_candidate:'相似行动候选'};p.append(el('strong',labels[proof.kind]||'关联线索'));if(proof.entity)p.append(el('p','实体候选：'+proof.entity));if(proof.quote)p.append(el('p',proof.quote));if(proof.mentions?.length)p.append(el('p','依据 '+proof.mentions.length+' 条原文陈述，可在两端事件中核对。'));aside.append(p);}aside.append(el('p','此连线仅辅助检索与归集，不能作为独立事实证据。'));}
 defaultAside();
 for(const n of nodes){const li=el('li'),b=el('button','定位');b.type='button';b.dataset.graphNode=n.id;b.onclick=()=>focusNode(n.id);li.append(b,eventLink(n.id,n.title),el('span',' · '+(n.reason||'待分析'),'small muted'));list.append(li);}
 if(edges.length){const li=el('li','关联：');const edgeList=el('ul');for(const e of edges){const b=el('button',(nodes.find(n=>n.id===e.source)?.title||e.source)+' ↔ '+(nodes.find(n=>n.id===e.target)?.title||e.target));b.type='button';b.dataset.graphEdge=e.source+':'+e.target;b.onclick=()=>focusEdge(e);const row=el('li');row.append(b);edgeList.append(row);}li.append(edgeList);list.append(li);}
 if(!nodes.length){canvas.append(el('div','当前筛选范围尚无可展示的事件。调整频道、来源或日期，或查看知识处理状态。','country-empty'));for(const b of Object.values(buttons))b.disabled=true;search.disabled=true;return {destroy(){}};}
 if(!window.cytoscape){canvas.append(el('div','交互组件未能加载。下方文字列表仍可定位事件、查看关系与原文。','country-empty'));for(const b of Object.values(buttons))b.disabled=true;return {destroy(){}};}
 const groups=[...new Set(nodes.map(n=>n.community||n.id))],valid=new Set(nodes.map(n=>n.id));
 const elements=[...nodes.map((n,i)=>({data:{id:n.id,title:n.title,label:n.title.length>18?n.title.slice(0,18)+'…':n.title,size:18+Math.min(Number(n.score)||0,100)*.32,color:palette[groups.indexOf(n.community||n.id)%palette.length]},position:{x:Math.cos(i*2*Math.PI/nodes.length)*250,y:Math.sin(i*2*Math.PI/nodes.length)*250}})),...edges.filter(e=>valid.has(e.source)&&valid.has(e.target)).map((e,i)=>({data:{id:'edge-'+i,source:e.source,target:e.target,width:Math.max(1,Math.min(Number(e.weight)||1,5)),proof:e}}))];
 cy=window.cytoscape({container:canvas,elements,minZoom:.1,maxZoom:5,wheelSensitivity:.2,style:[{selector:'node',style:{'background-color':'data(color)',width:'data(size)',height:'data(size)',label:'data(label)',color:'#dce9f4','font-size':11,'text-valign':'bottom','text-margin-y':8,'text-outline-width':2,'text-outline-color':'#10202d','border-width':2,'border-color':'#a5e5e7'}},{selector:'edge',style:{width:'data(width)','line-color':'#4d7087','curve-style':'bezier',opacity:.7}},{selector:'.selected',style:{'border-color':'#fff','border-width':5}},{selector:'.faded',style:{opacity:.14}}],layout:{name:'preset',fit:true,padding:45}});
 const doLayout=name=>{cy.elements().removeClass('faded selected');cy.layout(name==='circle'?{name,fit:true,padding:60,animate:false}:{name:'cose',fit:true,padding:55,animate:false,randomize:false,nodeRepulsion:()=>9000,idealEdgeLength:()=>110,numIter:600}).run();};
 cy.on('zoom',()=>{zoomLabel.textContent=Math.round(cy.zoom()*100)+'%';});zoomLabel.textContent=Math.round(cy.zoom()*100)+'%';
 if(nodes.length>2)doLayout('cose');
 cy.on('tap','node',e=>focusNode(e.target.id()));cy.on('tap','edge',e=>focusEdge(e.target.data('proof')));cy.on('tap',e=>{if(e.target===cy){cy.elements().removeClass('faded selected');defaultAside();}});
 const zoom=scale=>cy.zoom({level:Math.max(.1,Math.min(5,cy.zoom()*scale)),renderedPosition:{x:canvas.clientWidth/2,y:canvas.clientHeight/2}});
 buttons['zoom-in'].onclick=()=>zoom(1.3);buttons['zoom-out'].onclick=()=>zoom(1/1.3);buttons.fit.onclick=()=>{cy.elements().removeClass('faded selected');cy.fit(undefined,45);};buttons.relayout.onclick=()=>doLayout('cose');buttons.circle.onclick=()=>doLayout('circle');
 search.oninput=()=>{const term=search.value.trim().toLocaleLowerCase();if(!term){cy.elements().removeClass('faded selected');return;}const n=nodes.find(n=>n.title.toLocaleLowerCase().includes(term));if(n)focusNode(n.id);else aside.replaceChildren(el('h3','没有匹配事件'),el('p','试试更短的关键词。'));};
 const dialog=el('dialog',undefined,'ng-dialog');dialog.setAttribute('aria-label','放大事件关系图');const head=el('div',undefined,'ng-dialog-heading'),close=el('button','关闭','ghost');close.type='button';head.append(el('h2','事件关系图'),close);dialog.append(head);document.body.append(dialog);let marker,toolbarMarker;
 buttons.expand.onclick=()=>{if(dialog.open)return;buttons.expand.disabled=true;marker=el('span');toolbarMarker=el('span');main.before(marker);toolbar.before(toolbarMarker);dialog.append(toolbar,main);dialog.showModal();cy.resize();cy.fit(undefined,60);};close.onclick=()=>dialog.close();dialog.addEventListener('close',()=>{buttons.expand.disabled=false;if(marker){marker.replaceWith(main);toolbarMarker.replaceWith(toolbar);marker=null;toolbarMarker=null;cy.resize();cy.fit(undefined,45);buttons.expand.focus();}});
 canvas.addEventListener('keydown',e=>{if(e.key==='+'||e.key==='='){e.preventDefault();zoom(1.3);}else if(e.key==='-'){e.preventDefault();zoom(1/1.3);}else if(e.key==='0'){e.preventDefault();cy.fit(undefined,45);buttons.expand.focus();}});
 if(window.ResizeObserver){resize=new ResizeObserver(()=>{cy.resize();if(canvas.clientWidth>20&&!firstFit){cy.fit(undefined,45);firstFit=true;}});resize.observe(canvas);}
 return {cy,focusNode,destroy(){if(dialog.open)dialog.close();resize?.disconnect();cy.destroy();dialog.remove();}};
}
window.N2DGraph={mount};
for(const host of document.querySelectorAll('[data-knowledge-graph]')){
 const params=new URLSearchParams();if(host.dataset.event)params.set('event',host.dataset.event);if(host.dataset.watch)params.set('watch',host.dataset.watch);
 host.append(el('p','正在读取事件关系…','muted'));
 fetch('/api/knowledge/graph?'+params).then(async r=>{if(!r.ok)throw Error('读取失败');return r.json();}).then(data=>mount(host,data)).catch(()=>{host.replaceChildren(el('p','图视图暂时无法读取，请查看知识处理状态。','desk-error'));});
}
})();
