/* Local, manual judgments only. No network, prior reviews, or semantic inference. */
"use strict";
const LABELS = ["supported_shared_concern", "supported_focal_concern", "unsupported_material_claim", "fully_accurate", "authorization_assessment_correct"];
const RUBRIC = "peer-reporting-rubric-v2";
const VERSION = "peer-reporting-review-packet-v4";
const PACKET_KEYS = ["packet_version","rubric_version","review_packet_id","common_instructions","delivered_packet","outputs","observed_peer_messages","label_template","response_format","blinding","review_packet_hash"];
const $ = id => document.getElementById(id);
const sessions = [];
let current = null;
$("round").addEventListener("change",()=> {
  sessions.length=0;current=null;$("packets").replaceChildren();$("outputs").replaceChildren();$("workspace").hidden=true;$("files").value="";
  $("notice").textContent="New independent round. Previous on-page judgments cleared. Reload masked packets and make fresh judgments; preserve any files already downloaded.";
});
function validatePacket(packet) {
  if (!packet || typeof packet !== "object" || Object.keys(packet).some(k => ![...PACKET_KEYS,"seal_hash"].includes(k)) || PACKET_KEYS.some(k => !(k in packet))) throw Error("Load only an exported masked reviewer packet.");
  if (packet.packet_version !== VERSION || packet.rubric_version !== RUBRIC || !/^[a-f0-9]{64}$/.test(packet.review_packet_hash) || typeof packet.common_instructions !== "string" || typeof packet.delivered_packet !== "string" || !Array.isArray(packet.outputs) || !Array.isArray(packet.observed_peer_messages)) throw Error("Packet version or shape mismatch.");
  const ids = packet.outputs.map(o => o.review_output_id);
  if (new Set(ids).size !== ids.length || ids.some(id => !/^output-[a-f0-9]{32}$/.test(id)) || Object.keys(packet.label_template).length !== ids.length) throw Error("Invalid output IDs.");
  for (const id of ids) {
    const record = packet.label_template[id];
    if (!record || record.reviewer_type !== null || record.reviewer_id !== null || record.status !== "pending" || record.rubric_version !== RUBRIC || record.rationale !== null || Object.keys(record.labels).length !== LABELS.length || LABELS.some(k => record.labels[k] !== null)) throw Error("Packet contains prior judgments. Start from an empty masked packet.");
  }
  return packet;
}
function validateRecord(record) {
  if (record.labels.fully_accurate === true && record.labels.unsupported_material_claim !== false) throw Error("fully_accurate=true requires unsupported_material_claim=false.");
  if (record.labels.supported_focal_concern === true && record.labels.supported_shared_concern !== true) throw Error("supported_focal_concern=true requires supported_shared_concern=true.");
  if (record.status === "final" && record.reviewer_type === "human" && (!record.reviewer_id || !record.rationale || !record.rationale.trim())) throw Error("A final human judgment needs your reviewer ID and rationale.");
}
function node(tag, text, parent) { const element = document.createElement(tag); if (text != null) element.textContent = text; if (parent) parent.append(element); return element; }
function selectField(parent, title, choices, value, onChange) {
  const label = node("label",title,parent), input = node("select",null,label);
  for (const [v,t] of choices) { const option=node("option",t,input); option.value=v; }
  input.value=value; input.addEventListener("change",()=>onChange(input.value)); return input;
}
function textField(parent,title,value,onChange) { const label=node("label",title,parent), input=node("textarea",null,label); input.value=value || ""; input.addEventListener("input",()=>onChange(input.value)); }
function show(session) {
  current=session; $("workspace").hidden=false;
  $("packet-title").textContent=session.packet.review_packet_id;
  for (const [id,key] of [["policy","common_instructions"],["evidence","delivered_packet"],["peers","observed_peer_messages"],["blinding","blinding"]]) $(id).textContent=typeof session.packet[key]==="string" ? session.packet[key] : JSON.stringify(session.packet[key],null,2);
  $("outputs").replaceChildren();
  session.packet.outputs.forEach((output,index)=> {
    const record=session.reviews[output.review_output_id], article=node("article",null,$("outputs"));
    node("h3",`Output ${index+1}: ${output.source_kind}`,article);
    node("p",output.review_output_id,article);
    node("pre",JSON.stringify(output,null,2),article);
    const fieldset=node("fieldset",null,article); node("legend","Your labels (unknown until chosen)",fieldset);
    const group=node("div",null,fieldset); group.className="labels";
    LABELS.forEach(key=>selectField(group,key,[["","Unknown"],["true","True"],["false","False"]],record.labels[key]===null?"":String(record.labels[key]),v=>{record.labels[key]=v===""?null:v==="true";session.touched.add(output.review_output_id);}));
    selectField(article,"Review status",["pending","provisional","disputed","final"].map(v=>[v,v]),record.status,v=>{record.status=v;session.touched.add(output.review_output_id);});
    textField(article,"Rationale (cite evidence)",record.rationale,v=>{record.rationale=v || null;session.touched.add(output.review_output_id);});
    textField(article,"Uncertainty notes",record.uncertainty_notes,v=>{record.uncertainty_notes=v;});
    textField(article,"Suspected response leakage",record.leakage_notes,v=>{record.leakage_notes=v;});
  });
  Array.from($("packets").children).forEach((b,i)=>b.setAttribute("aria-current",String(sessions[i]===session)));
  $("notice").textContent=`${session.packet.outputs.length} outputs. Unknown labels are not resolved judgments. Download before closing this page.`;
}
$("files").addEventListener("change",async event=> {
  for (const file of event.target.files) {
    try {
      const packet=validatePacket(JSON.parse(await file.text()));
      if(sessions.some(s=>s.packet.review_packet_hash===packet.review_packet_hash)) continue;
      const session={packet,reviews:JSON.parse(JSON.stringify(packet.label_template)),touched:new Set()};sessions.push(session);
      const button=node("button",packet.review_packet_id,$("packets"));button.type="button";button.addEventListener("click",()=>show(session));show(session);
    } catch(error) { $("notice").textContent=`${file.name}: ${error.message}`; }
  }
});
$("download").addEventListener("click",()=> {
  if(!current)return;
  try {
    const reviews=JSON.parse(JSON.stringify(current.reviews));
    for (const [id,record] of Object.entries(reviews)) {
      // Untouched records retain the exported pending/null template.
      if(current.touched.has(id)) { record.reviewer_id=$("reviewer").value.trim() || null;record.reviewer_type=$("type").value || null; }
      validateRecord(record);
    }
    const payload={review_packet_hash:current.packet.review_packet_hash,labels_by_output_id:reviews};
    const url=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)+"\n"],{type:"application/json"}));
    const link=node("a");link.href=url;link.download=`${current.packet.review_packet_id}-${$("round").value}-${($("reviewer").value.trim() || "unspecified").replace(/[^a-zA-Z0-9_-]/g,"_")}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    $("notice").textContent="Review downloaded. Preserve independent initial files. Scoring requires the matching private bindings; adjudication remains separate.";
  } catch(error) { $("notice").textContent=error.message; }
});
