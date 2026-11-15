/* Explicit, asynchronous reading aid. Originals and evidence are never replaced. */
(() => {
  'use strict';
  const sleep = ms => new Promise(resolve => setTimeout(resolve,ms));
  const errorText = detail => typeof detail==='string'?detail:Array.isArray(detail)?detail.map(item=>item.msg||'请求无效').join('；'):'翻译请求未完成，请稍后重试。';
  async function request(url,body){
    const response=await fetch(url,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
    let data;try{data=await response.json();}catch{throw Error('服务暂不可用，请稍后重试。');}
    if(!response.ok)throw Error(errorText(data.detail));
    return data;
  }
  for(const host of document.querySelectorAll('[data-translation]')){
    const button=host.querySelector('[data-translation-start]'),status=host.querySelector('[data-translation-status]'),result=host.querySelector('[data-translation-result]');
    if(!button||!status||!result)continue;
    let failed=false,running=false;
    const defaultLabel=button.textContent;
    button.addEventListener('click',async()=>{
      if(running)return;
      running=true;button.disabled=true;status.textContent='正在提交翻译…';
      try{
        const payload={kind:host.dataset.translationKind,key:host.dataset.translationKey,target:'zh-CN',scope:host.dataset.translationScope||'document'};
        if(host.dataset.translationRevision)payload.revision=Number(host.dataset.translationRevision);
        if(failed)payload.retry=true;
        let data=await request('/api/translations',payload);
        const deadline=Date.now()+180000;
        while(['queued','pending','running','waiting'].includes(data.status)){
          if(Date.now()>deadline)throw Error('翻译仍在后台处理。可稍后再次点击查看结果，原文仍可正常阅读。');
          status.textContent=data.status==='running'?'正在翻译，原文仍可阅读…':'已排队，正在等待翻译…';
          await sleep(1200);
          data=await request('/api/translations/'+encodeURIComponent(data.id));
        }
        if(data.status!=='succeeded'||typeof data.translated_text!=='string'||!data.translated_text.trim())throw Error(data.error||'本次翻译未完成，请重试。');
        host.querySelector('[data-translation-text]').textContent=data.translated_text;
        const original=host.querySelector('[data-translation-original]');if(original)original.textContent=typeof data.original==='string'?data.original:'';
        const note=host.querySelector('[data-translation-note]');if(note)note.textContent='机器翻译仅供阅读辅助，不作为核实证据。'+(data.truncated?'本次只翻译了已保存内容的前一部分。':'')+(data.revision?'对应材料版本 r'+data.revision+'。':'');
        result.hidden=false;status.textContent='翻译完成';button.textContent='重新查看译文';failed=false;
      }catch(error){
        failed=true;status.textContent=error.message;button.textContent='重试翻译';
        if(!host.querySelector('[data-translation-settings]')){const settings=document.createElement('a');settings.href='/setup#model-card';settings.textContent='检查翻译模型配置';settings.className='small';settings.dataset.translationSettings='';status.append(settings);}
      }finally{running=false;button.disabled=false;if(!button.textContent)button.textContent=defaultLabel;}
    });
  }
})();
