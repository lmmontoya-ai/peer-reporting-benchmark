'use strict';
const $=id=>document.getElementById(id);
const sum=(rows,key)=>rows.reduce((n,r)=>n+(Number(r[key])||0),0);
const fmt=n=>n.toLocaleString('en-US');
function td(tr,value){const el=document.createElement('td');el.textContent=String(value);tr.append(el);return el;}
async function load(){
 const response=await fetch('results-v1/summary.json');if(!response.ok)throw new Error('Summary unavailable');const data=await response.json();
 const indexResponse=await fetch('collection-v1/index.json');if(!indexResponse.ok)throw new Error('Evidence index unavailable');const index=await indexResponse.json();
 if(data.publication_kind!=='redacted_public_peer_results_v1'||index.kind!=='live_evidence_review_export')throw new Error('Unexpected public artifact format');
 const rows=data.rows,collection=rows.filter(r=>r.split==='collection'),smoke=rows.filter(r=>r.split==='smoke');
 const evidence=new Map(index.rows.map(r=>[r.assignment_id,r.evidence_page]));
 $('totals').replaceChildren();for(const [value,label] of [[collection.length,'collection assignments'],[smoke.length,'separate smoke assignments'],[sum(collection,'accepted_report_count'),'accepted collection reports'],[sum(collection,'pending_output_count'),'outputs pending human review'],[sum(collection,'final_human_output_count'),'final human labels']]){const card=document.createElement('div');card.className='card';const strong=document.createElement('strong');strong.textContent=fmt(value);const span=document.createElement('span');span.textContent=label;card.append(strong,span);$('totals').append(card);}
 for(const [N,K] of [[16,1],[100,1],[100,0]]){const group=collection.filter(r=>r.N===N&&r.K===K),tr=document.createElement('tr');[`${N} peers / ${K} prohibited operation${K===1?'':'s'}`,group.length,`${group.filter(r=>r.report_attempt_count>0).length}/${group.length}`,`${group.filter(r=>r.accepted_report_count>0).length}/${group.length}`,`${group.filter(r=>r.task_outcome===true).length}/${group.length}`].forEach(v=>td(tr,v));$('cells').append(tr);}
 for(const [id,key] of [['model','model'],['prompt','prompt_condition']])for(const v of [...new Set(rows.map(r=>r[key]))].sort()){const option=document.createElement('option');option.value=v;option.textContent=v;$(id).append(option);}
 const tokens=group=>group.reduce((n,r)=>n+(r.resource.usage_total_tokens||0),0);const unknown=rows.filter(r=>r.resource.usage_settlement!=='settled').length;
 $('resources').textContent=`Collection: ${fmt(tokens(collection))} known settled tokens. Smoke: ${fmt(tokens(smoke))} known settled tokens. ${unknown} assignments have unresolved final usage.`;
 function link(row){const path=evidence.get(row.assignment_id);if(!/^evidence\/assignment-\d{4}\.html$/.test(path||''))throw new Error('Unsafe evidence link');const a=document.createElement('a');a.href=`collection-v1/${path}`;a.textContent='Read evidence';return a;}
 function render(){const selected=rows.filter(r=>r.split===$('split').value&&(!$('cell').value||`${r.N}/${r.K}`===$('cell').value)&&(!$('model').value||r.model===$('model').value)&&(!$('prompt').value||r.prompt_condition===$('prompt').value)&&(!$('block').value||String(r.block)===$('block').value)).sort((a,b)=>a.planned_order-b.planned_order);$('selection').textContent=`${selected.length} assignments; ${selected.filter(r=>r.report_attempt_count>0).length} attempted a report; ${selected.filter(r=>r.accepted_report_count>0).length} had an accepted report; ${sum(selected,'pending_output_count')} outputs await human review.`;$('rows').replaceChildren();$('first').replaceChildren();if(selected.length){$('first').append('First selected assignment in planned order: ',link(selected[0]));}for(const r of selected){const tr=document.createElement('tr');[`${r.model} / ${r.prompt_condition}`,`${r.N} / ${r.K} / ${r.block}`,r.report_attempt_count,r.accepted_report_count,r.task_outcome===true?'correct':r.task_outcome===false?'incorrect':'unknown',r.pending_output_count].forEach(v=>td(tr,v));const cell=td(tr,'');cell.append(link(r));$('rows').append(tr);}}
 $('filters').addEventListener('change',render);$('filters').addEventListener('submit',e=>e.preventDefault());render();
}
load().catch(error=>{$('totals').textContent=`Unable to load public artifacts: ${error.message}`;});

