"""Drop-in backend for the external MPP_Project Graph RAG frontend.

The external project (techki545/MPP_Project) is a dependency-free HTTP UI with
its own static evidence graph. This bridge serves that same frontend and
implements its `/api/health`, `/api/evidence`, `/api/analyze` and
`/api/model/test` contract on top of the mppkb kernel (cold/hot claims, P0,
citations, refusal, audit, review queue, LangGraph case runtime).

It also injects a small kernel panel into the served HTML so the review queue,
P0 todo counts and a complex-case runner are reachable without editing the
external repository.

Run:
    python -m mppkb.ui_bridge --repo C:\\path\\to\\MPP_Project --port 8791
"""

from __future__ import annotations

import argparse
import csv
import difflib
import io
import json
import mimetypes
import os
import smtplib
import threading
import time
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .answer import EvidenceService, answer
from .config import Config, load_config
from .db import ensure_databases
from .graph import run_case
from .promote import (
    REASON_CODES,
    assignment_sla,
    assign_batch,
    batch_decide,
    batch_impact,
    decide,
    list_assignments,
    list_queue,
    purge_answer_questions,
    review_ledger,
    review_sla_hours,
    review_sla_warn_hours,
    reviewers as list_reviewers,
    reviews_csv,
    writeback_answers,
)
from .todos import collect as collect_todos

DEFAULT_QUESTION = "儿童重症支原体肺炎如何选择抗感染治疗？"
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/styles.css": "styles.css",
}
ALLOWED_TYPES = (
    "guideline",
    "systematic_review",
    "randomized_controlled_trial",
    "observational_study",
    "narrative_review",
    "case_report",
)
TYPE_LABELS = {
    "guideline": "指南",
    "systematic_review": "系统综述/Meta 分析",
    "randomized_controlled_trial": "随机对照试验（RCT）",
    "observational_study": "观察性研究",
    "narrative_review": "叙述性综述",
    "case_report": "病例报告",
}
REASON_LABELS = {
    "insufficient_evidence": "证据不足",
    "extraction_error": "抽取错误",
    "term_mismatch": "术语不匹配",
    "duplicate": "重复",
    "outdated": "过时",
    "other": "其他",
}
RELATION_ORDER = ("supports", "updates", "supplements", "confirms", "cautions")

INJECT_HTML = """
<style>
#kb-fab{position:fixed;right:18px;bottom:18px;z-index:99999;background:#176b4d;color:#fff;border:0;border-radius:24px;padding:10px 16px;font:13px system-ui;box-shadow:0 4px 14px rgba(0,0,0,.25);cursor:pointer}
#kb-panel{position:fixed;right:18px;bottom:74px;width:540px;max-height:80vh;overflow:auto;background:#fff;border:1px solid #d7dde4;border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.25);padding:12px;font:12px/1.5 system-ui;display:none;z-index:99999}
#kb-panel h3{margin:0 0 8px;font-size:14px;color:#176b4d}
#kb-todos{font-weight:600;color:#315f75;margin-bottom:8px}
#kb-actions{margin:6px 0}
#kb-actions button{margin-right:6px;font-size:11px;padding:3px 9px;border:1px solid #b9c2cb;background:#f7f9fa;border-radius:4px;cursor:pointer}
#kb-filters{display:flex;gap:4px;flex-wrap:wrap;margin:6px 0}
#kb-filters select,#kb-filters input{font-size:11px;max-width:130px}
#kb-filters button{font-size:11px}
#kb-selectbar{display:flex;gap:8px;align-items:center;margin:4px 0;font-size:11px}
#kb-selectbar button{font-size:11px;padding:2px 8px;border:1px solid #b9c2cb;background:#f7f9fa;border-radius:4px;cursor:pointer}
#kb-selcount{color:#315f75;font-weight:600}
#kb-reason{font-size:11px;max-width:170px}
#kb-assignbar{display:flex;gap:6px;align-items:center;margin:4px 0;font-size:11px}
#kb-assignbar input{font-size:11px;width:90px}
#kb-assignbar button{font-size:11px;padding:2px 8px;border:1px solid #b9c2cb;background:#f7f9fa;border-radius:4px;cursor:pointer}
#kb-ledger{display:none;white-space:pre-wrap;background:#f7f9fa;padding:6px;max-height:170px;overflow:auto;font-size:10px;border:1px solid #eef1f4}
#kb-history{display:none;margin:6px 0;border-top:1px dashed #d9e2ec;padding-top:4px}
.kb-hist-row{font-size:10px;border-bottom:1px solid #f0f2f4;padding:4px 0}
.kb-hist-row button{margin-left:4px;font-size:10px;padding:1px 6px;border:1px solid #b9c2cb;background:#f7f9fa;border-radius:3px;cursor:pointer}
#kb-history-result{display:none;white-space:pre-wrap;background:#f7f9fa;padding:6px;max-height:150px;overflow:auto;font-size:10px}
#kb-compare{display:none;white-space:pre-wrap;background:#f7f9fa;padding:6px;max-height:180px;overflow:auto;font-size:10px;border:1px solid #eef1f4}
#kb-impact{display:none;font-size:11px;color:#7a4b00;background:#fff8e6;border:1px solid #f0d9a8;padding:5px;border-radius:4px;margin:6px 0;white-space:pre-wrap}
#kb-pageinfo{font-size:11px;color:#666;margin:4px 0}
.kb-group{font-weight:700;color:#315f75;border-bottom:1px solid #d9e2ec;padding:4px 0;margin-top:4px}
.kb-row{border-top:1px solid #eef1f4;padding:6px 0}
.kb-row input{margin-right:6px}
#kb-pager{display:flex;gap:8px;align-items:center;margin-top:6px}
#kb-pager button{font-size:11px;padding:3px 9px;border:1px solid #b9c2cb;background:#f7f9fa;border-radius:4px;cursor:pointer}
#kb-case{width:100%;box-sizing:border-box;font:11px/1.4 Consolas,monospace;margin-top:8px}
.kb-form{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:8px}
.kb-form label{font-size:11px;color:#315f75;display:flex;flex-direction:column;gap:2px}
.kb-form input,.kb-form select{font:12px system-ui;padding:4px 6px;border:1px solid #d7dde4;border-radius:4px}
#kb-case-simple,#kb-case-risk{margin-top:6px;background:#315f75;color:#fff;border:0;border-radius:4px;padding:5px 10px;cursor:pointer;margin-right:6px}
#kb-run{margin-top:6px;background:#176b4d;color:#fff;border:0;border-radius:4px;padding:5px 10px;cursor:pointer}
#kb-result{white-space:pre-wrap;background:#f7f9fa;padding:6px;max-height:160px;overflow:auto;font-size:11px}
.kb-source-link{color:#176b4d;font-weight:600;text-decoration:underline;cursor:pointer}
#kb-source-pop{position:fixed;z-index:100001;display:none;width:min(390px,90vw);max-height:52vh;overflow:auto;background:#fff;border:1px solid #d7dde4;border-radius:10px;box-shadow:0 12px 34px rgba(0,0,0,.28);padding:10px 12px;font:12px/1.55 system-ui}
#kb-source-pop h4{margin:0 0 6px;font-size:13px;color:#176b4d;padding-right:22px}
#kb-source-pop .kb-src-meta{color:#315f75;font-weight:600;margin-bottom:4px}
#kb-source-pop .kb-src-quote{color:#333;background:#f7f9fa;border-left:3px solid #9fb8c8;padding:6px 8px;margin-top:6px;white-space:pre-wrap}
#kb-source-pop .kb-src-close{position:absolute;right:8px;top:6px;border:0;background:transparent;font-size:16px;cursor:pointer;color:#666}
#kb-source-pop .kb-src-detail{margin-top:8px;border:1px solid #b9c2cb;background:#f7f9fa;border-radius:4px;padding:4px 10px;font:11px system-ui;cursor:pointer;color:#315f75}
#kb-source-modal-wrap{position:fixed;inset:0;z-index:100002;display:none;align-items:center;justify-content:center;background:rgba(20,30,36,.45);padding:20px}
#kb-source-modal{position:relative;width:min(760px,92vw);max-height:84vh;overflow:auto;background:#fff;border-radius:10px;box-shadow:0 18px 50px rgba(0,0,0,.35);padding:18px 20px;font:12.5px/1.65 system-ui;color:#26343d}
#kb-source-modal h3{margin:0 0 6px;font-size:15px;color:#176b4d;padding-right:28px}
#kb-source-modal .kb-modal-meta{color:#315f75;font-weight:600;margin-bottom:8px}
#kb-source-modal .kb-modal-full{white-space:pre-wrap;background:#f7f9fa;border-left:3px solid #9fb8c8;padding:10px 12px;margin-top:8px}
#kb-source-modal .kb-modal-close{position:absolute;right:10px;top:8px;border:0;background:transparent;font-size:22px;cursor:pointer;color:#666}
#kb-source-modal .kb-modal-hint{color:#68767f;font-size:11px;margin-top:8px}
#kb-evidence-status{margin:10px 0 0;border:1px solid #d7dde4;border-radius:6px;background:#f7f9fa;padding:7px 10px;font:11px/1.6 system-ui;color:#3b4a52}
#kb-evidence-status summary{cursor:pointer;color:#315f75;font-weight:600}
#kb-evidence-status .kb-status-row{display:flex;gap:8px;border-top:1px solid #e6ebef;padding:3px 0}
#kb-evidence-status .kb-status-label{color:#68767f;min-width:72px}
#kb-evidence-status .kb-status-value{white-space:pre-wrap}
#evidence-graph .edge-label{display:none}
#evidence-graph .graph-edge{opacity:1 !important;stroke-width:2.2 !important}
#evidence-graph .graph-edge.is-muted{opacity:1 !important}
#evidence-graph .graph-node.is-muted{opacity:1 !important}
#evidence-graph .node-title{font-size:10.5px}
#evidence-graph .graph-edge.kb-edge-active{opacity:1 !important;stroke-width:2.8 !important}
#evidence-graph .graph-edge.kb-edge-dim{opacity:.16 !important}
</style>
<button id="kb-fab">内核审核 / 病例</button>
<div id="kb-panel">
  <h3>mppkb 内核</h3>
  <div id="kb-todos">加载中…</div>
  <div id="kb-slainfo" style="font-size:11px;color:#a3443d;margin-bottom:6px"></div>
  <div id="kb-actions">
    <button id="kb-ba">批量通过</button>
    <button id="kb-br">批量拒绝</button>
    <button id="kb-preview">影响预览</button>
    <select id="kb-reason"><option value="">理由（可选）</option></select>
    <input id="kb-note" placeholder="自定义理由" size="10">
    <button id="kb-csv">导出 CSV</button>
  </div>
  <div id="kb-filters">
    <select id="kb-group">
      <option value="intervention">按药物分组</option>
      <option value="severity">按严重度分组</option>
      <option value="none">不分组</option>
    </select>
    <select id="kb-drug"><option value="">全部药物</option></select>
    <select id="kb-sev"><option value="">全部严重度</option></select>
    <select id="kb-revfilter"><option value="">全部审核人</option></select>
    <select id="kb-sla"><option value="">全部SLA</option><option value="overdue">已超时</option><option value="due_soon">即将到期</option><option value="on_time">正常</option></select>
    <input id="kb-q" placeholder="搜索" size="8">
    <button id="kb-apply">筛选</button>
  </div>
  <div id="kb-selectbar">
    <button id="kb-selectall">全选筛选结果</button>
    <button id="kb-clear">清空选择</button>
    <span id="kb-selcount">已选 0 条</span>
  </div>
  <div id="kb-assignbar">
    <input id="kb-assignee" placeholder="审核人">
    <button id="kb-assign">分配 / 转派选中</button>
    <button id="kb-ledger-btn">审核台账</button>
    <button id="kb-export-reviews">导出审核结果</button>
    <button id="kb-notify">SLA 通知</button>
  </div>
  <pre id="kb-ledger"></pre>
  <div id="kb-pageinfo"></div>
  <div id="kb-impact"></div>
  <div id="kb-history"></div>
  <button id="kb-regen-all" style="display:none;font-size:11px;margin:2px 0">重生成全部受影响答案</button>
  <pre id="kb-history-result"></pre>
  <pre id="kb-compare"></pre>
  <div id="kb-queue"></div>
  <div id="kb-pager">
    <button id="kb-prev">上一页</button>
    <span id="kb-page"></span>
    <button id="kb-next">下一页</button>
  </div>
  <div class="kb-form">
    <label>问题 <input id="kb-case-question" value="儿童 重症 阿奇霉素 剂量" /></label>
    <label>年龄（岁） <input id="kb-case-age" type="number" min="0" max="120" value="5" /></label>
    <label>药物 <input id="kb-case-drug" value="阿奇霉素" /></label>
    <label>严重度
      <select id="kb-case-severity">
        <option value="">未选择</option>
        <option value="severe">重症</option>
        <option value="refractory">难治性</option>
        <option value="fulminant">暴发</option>
        <option value="critical">危重</option>
      </select>
    </label>
  </div>
  <div>
    <button id="kb-case-simple">简单病例</button>
    <button id="kb-case-risk">高风险病例</button>
    <button id="kb-run">用上面条件运行</button>
  </div>
  <pre id="kb-result"></pre>
</div>
<script>
(function(){
  const $=id=>document.getElementById(id);
  const QUEUE=[]; const SEL_MAP=new Map();
  let HISTORY=[];
  let PAGE=1, PAGES=1, TOTAL=0, GROUP='intervention', DRUG='', SEV='', Q='', ASSIGNEE='', SLA='';
  let REASONS=[];
  let sourceLinkTimer=null;
  window.__KB_SOURCES=[];
  if(window.fetch){
    const originalFetch=window.fetch;
    window.fetch=async function(...args){
      const url=String((typeof args[0]==='string'?args[0]:args[0]&&args[0].url)||'');
      const response=await originalFetch.apply(this,args);
      if(url.includes('/api/analyze')){
        try{
          response.clone().json().then(data=>{
            window.__KB_SOURCES=data.source_details||[];
            window.__KB_ANALYSIS=data;
            if(window.__KB_SHOW_GRAPH) window.__KB_SHOW_GRAPH();
            scheduleSourceLinks();
            setTimeout(()=>renderEvidenceStatusDetails(data),80);
          }).catch(()=>{});
        }catch(e){}
      }
      return response;
    };
  }
  function ensureSourcePop(){
    let pop=$('kb-source-pop');
    if(pop) return pop;
    pop=document.createElement('div');
    pop.id='kb-source-pop';
    const close=document.createElement('button');
    close.className='kb-src-close'; close.textContent='×';
    close.onclick=()=>{pop.style.display='none';};
    pop.appendChild(close);
    document.body.appendChild(pop);
    return pop;
  }
  function openSourceModal(titleText,metaText,fullText){
    let wrap=$('kb-source-modal-wrap');
    if(!wrap){
      wrap=document.createElement('div'); wrap.id='kb-source-modal-wrap';
      const modal=document.createElement('div'); modal.id='kb-source-modal';
      const close=document.createElement('button'); close.className='kb-modal-close'; close.textContent='×';
      close.onclick=()=>{wrap.style.display='none';};
      modal.appendChild(close);
      const title=document.createElement('h3'); title.id='kb-modal-title'; modal.appendChild(title);
      const meta=document.createElement('div'); meta.className='kb-modal-meta'; meta.id='kb-modal-meta'; modal.appendChild(meta);
      const full=document.createElement('div'); full.className='kb-modal-full'; full.id='kb-modal-full'; modal.appendChild(full);
      const hint=document.createElement('div'); hint.className='kb-modal-hint'; hint.textContent='原文直接来自来源文档，仅用于核对；是否采纳仍需结合上下文判断。'; modal.appendChild(hint);
      wrap.appendChild(modal); document.body.appendChild(wrap);
      wrap.addEventListener('click',event=>{if(event.target===wrap) wrap.style.display='none';});
    }
    $('kb-modal-title').textContent=titleText||'来源详情';
    $('kb-modal-meta').textContent=metaText||'';
    $('kb-modal-full').textContent=fullText||'（无原文文本）';
    wrap.style.display='flex';
  }
  function sourceMeta(detail){
    return '文档 '+(detail.doc_id||'未标注')+' · 第 '+(detail.page==null?'?':detail.page)+' 页'
      +(detail.evidence_type?' · '+detail.evidence_type:'');
  }
  function showSourcePop(anchor,sourceNo){
    const pop=ensureSourcePop();
    const detail=(window.__KB_SOURCES||[]).find(item=>String(item.source_no)===String(sourceNo));
    pop.replaceChildren();
    const close=document.createElement('button');
    close.className='kb-src-close'; close.textContent='×';
    close.onclick=()=>{pop.style.display='none';};
    pop.appendChild(close);
    const title=document.createElement('h4');
    title.textContent='来源 Source['+sourceNo+']';
    pop.appendChild(title);
    if(!detail){
      const empty=document.createElement('div');
      empty.textContent='来源详情尚未加载，请重新生成回答后再点。';
      pop.appendChild(empty);
    }else{
      const meta=document.createElement('div');
      meta.className='kb-src-meta';
      meta.textContent=sourceMeta(detail);
      pop.appendChild(meta);
      const text=document.createElement('div');
      text.textContent=detail.text||'';
      pop.appendChild(text);
      const fullQuote=String(detail.quote||'');
      if(fullQuote){
        const quote=document.createElement('div');
        quote.className='kb-src-quote';
        quote.textContent='原文：'+(fullQuote.length>260?fullQuote.slice(0,260)+'…':fullQuote);
        pop.appendChild(quote);
        const detailButton=document.createElement('button');
        detailButton.className='kb-src-detail';
        detailButton.textContent='详情（查看全文）';
        detailButton.onclick=()=>openSourceModal('来源 Source['+sourceNo+']',sourceMeta(detail),fullQuote);
        pop.appendChild(detailButton);
      }
    }
    const rect=anchor.getBoundingClientRect();
    const width=Math.min(390,window.innerWidth*0.9);
    pop.style.left=Math.max(8,Math.min(rect.left,window.innerWidth-width-8))+'px';
    pop.style.top=Math.min(rect.bottom+8,window.innerHeight-220)+'px';
    pop.style.display='block';
  }
  function enhanceSourceLinks(){
    const root=document.getElementById('final-answer');
    if(!root||!root.querySelectorAll) return;
    const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,null,false);
    const nodes=[];
    while(walker.nextNode()){
      const node=walker.currentNode;
      if(!node.nodeValue||!node.nodeValue.includes('Source[')) continue;
      if(node.parentElement&&node.parentElement.closest('.kb-source-link,#kb-source-pop')) continue;
      nodes.push(node);
    }
    nodes.forEach(node=>{
      const text=node.nodeValue;
      const regex=/Source\\[(\\d+)\\]/g;
      let match; let last=0; let changed=false;
      const fragment=document.createDocumentFragment();
      while((match=regex.exec(text))){
        changed=true;
        if(match.index>last) fragment.appendChild(document.createTextNode(text.slice(last,match.index)));
        const link=document.createElement('a');
        link.href='#';
        link.className='kb-source-link';
        link.dataset.source=match[1];
        link.textContent=match[0];
        fragment.appendChild(link);
        last=regex.lastIndex;
      }
      if(!changed) return;
      if(last<text.length) fragment.appendChild(document.createTextNode(text.slice(last)));
      node.parentNode.replaceChild(fragment,node);
    });
  }
  function scheduleSourceLinks(){
    if(sourceLinkTimer) clearTimeout(sourceLinkTimer);
    sourceLinkTimer=setTimeout(enhanceSourceLinks,60);
  }
  document.addEventListener('click',event=>{
    const link=event.target.closest&&event.target.closest('.kb-source-link');
    if(link){
      event.preventDefault();
      showSourcePop(link,link.dataset.source);
      return;
    }
    const pop=document.getElementById('kb-source-pop');
    if(pop&&!event.target.closest('#kb-source-pop')) pop.style.display='none';
  });
  const sourceObserver=new MutationObserver(()=>scheduleSourceLinks());
  sourceObserver.observe(document.body,{childList:true,subtree:true});
  document.addEventListener('DOMContentLoaded',scheduleSourceLinks);
  scheduleSourceLinks();
  const KIND_LABELS={replace:'替换',new:'新增',evidence_update:'证据更新',noop:'无变化',reject:'拒绝保留',hold:'挂起'};
  function esc(v){return String(v==null?'':v).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
  function key(it){return String(it.claim_id)+'|'+String(it.version_id);}
  function selectedItems(){return Array.from(SEL_MAP.values());}
  async function j(url,opt){const r=await fetch(url,opt||{}); if(!r.ok) throw new Error(await r.text()); return r.json();}
  function filterParams(){const params=new URLSearchParams(); if(DRUG)params.set('intervention',DRUG); if(SEV)params.set('severity',SEV); if(Q)params.set('q',Q); if(ASSIGNEE)params.set('assignee',ASSIGNEE); if(SLA)params.set('sla',SLA); return params;}
  $('kb-fab').onclick=()=>{const p=$('kb-panel'); p.style.display=p.style.display==='block'?'none':'block'; if(p.style.display==='block'){loadTodos();loadQueue();}};
  async function loadTodos(){try{const d=await j('/api/todos'); $('kb-todos').textContent='KB 待审 '+d.kb_queue+' · P0 补审 '+d.p0_queue+' · 病例 Gate '+d.case_gate;}
    catch(e){$('kb-todos').textContent='待办加载失败：'+e.message;}}
  async function loadSla(){try{const d=await j('/api/sla'); $('kb-slainfo').textContent='SLA('+d.hours+'h)：超时 '+d.overdue+' · 即将到期 '+d.due_soon;}
    catch(e){$('kb-slainfo').textContent='SLA 加载失败';}}
  async function loadReasons(){try{const d=await j('/api/reason-codes'); REASONS=d.items||[]; const sel=$('kb-reason');
    sel.innerHTML='<option value="">理由（可选）</option>';
    REASONS.forEach(r=>{const o=document.createElement('option');o.value=r.code;o.textContent=r.label;sel.appendChild(o);});}
    catch(e){}}
  function updateSelCount(){$('kb-selcount').textContent='已选 '+SEL_MAP.size+' 条';}
  function renderHistory(){const box=$('kb-history'); box.innerHTML=''; if(!HISTORY.length){box.style.display='none';$('kb-regen-all').style.display='none';return;}
    box.style.display='block'; $('kb-regen-all').style.display='inline-block';
    const seen=new Set();
    HISTORY.forEach(h=>{if(seen.has(h.seq))return; seen.add(h.seq);
      const row=document.createElement('div'); row.className='kb-hist-row';
      const diff=(h.diffs||[]).map(d=>'旧 '+JSON.stringify(d.old_value)+' → 新 '+JSON.stringify(d.new_value)).join('；');
      row.innerHTML='<b>#'+h.seq+'</b> '+esc(h.question||'(旧日志无问题文本)')+'<br>'+esc(diff);
      const btn=document.createElement('button'); btn.textContent='重生成'; btn.onclick=()=>regenerate(h.seq);
      const cmp=document.createElement('button'); cmp.textContent='对比'; cmp.onclick=()=>compare(h.seq);
      const wb=document.createElement('button'); wb.textContent='回写'; wb.onclick=()=>writeback(h.claim_id,(h.matched_version_ids||[])[0]);
      row.append(btn,cmp,wb); box.appendChild(row);});
  }
  async function regenerate(seq){try{$('kb-history-result').style.display='block';$('kb-history-result').textContent='重生成中…';
      const d=await j('/api/answers/regenerate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({seq:seq})});
      const r=(d.results&&d.results[0])||d; $('kb-history-result').textContent='#'+seq+' 重生成：refused='+r.refused+' 引用='+r.citations+'\\n'+(r.answer||'');}
    catch(e){$('kb-history-result').textContent='重生成失败：'+e.message;}}
  async function regenerateAll(){if(!HISTORY.length)return;
    const seqs=Array.from(new Set(HISTORY.map(h=>h.seq)));
    try{$('kb-history-result').style.display='block';$('kb-history-result').textContent='批量重生成 '+seqs.length+' 条…';
      const d=await j('/api/answers/regenerate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({seqs:seqs})});
      $('kb-history-result').textContent='批量重生成：成功 '+d.succeeded+' / 失败 '+d.failed;}
    catch(e){$('kb-history-result').textContent='批量重生成失败：'+e.message;}}
  async function compare(seq){try{$('kb-compare').style.display='block';$('kb-compare').textContent='对比中…';
      const d=await j('/api/answers/compare',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({seq:seq})});
      if(!d.new){$('kb-compare').textContent='#'+seq+' 尚无新版本，可先点「重生成」';return;}
      const diff=(d.diff&&d.diff.text)||[];
      const changed=((d.diff&&d.diff.claims_changed)||[]).map(c=>c.claim_id+': '+JSON.stringify(c.old_value)+' → '+JSON.stringify(c.new_value));
      $('kb-compare').textContent='旧 #'+d.old.seq+' ('+(d.old.library_version||'-')+') → 新 #'+d.new.seq+' ('+(d.new.library_version||'-')+')\\n'+
        'claim 变化：\\n'+(changed.join('\\n')||'无')+'\\n文本 diff：\\n'+(diff.join('\\n')||'无');}
    catch(e){$('kb-compare').style.display='block';$('kb-compare').textContent='对比失败：'+e.message;}}
  async function writeback(claim_id,version_id){try{$('kb-compare').style.display='block';$('kb-compare').textContent='回写中…';
      const d=await j('/api/answers/writeback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({claim_id:claim_id,version_id:version_id})});
      $('kb-compare').textContent='回写完成：written='+(d.written||0); refreshImpact();}
    catch(e){$('kb-compare').style.display='block';$('kb-compare').textContent='回写失败：'+e.message;}}
  function fillFacets(f){
    const drug=$('kb-drug'), sev=$('kb-sev');
    drug.innerHTML='<option value="">全部药物</option>';
    (f.interventions||[]).forEach(x=>{const o=document.createElement('option');o.value=x.key;o.textContent=x.label+' ('+x.count+')';if(x.key===DRUG)o.selected=true;drug.appendChild(o);});
    sev.innerHTML='<option value="">全部严重度</option>';
    (f.severities||[]).forEach(x=>{const o=document.createElement('option');o.value=x.key;o.textContent=x.label+' ('+x.count+')';if(x.key===SEV)o.selected=true;sev.appendChild(o);});
    const rev=$('kb-revfilter'); rev.innerHTML='<option value="">全部审核人</option>';
    (f.reviewers||[]).forEach(x=>{const o=document.createElement('option');o.value=x.key;o.textContent=x.label+' ('+x.count+')';if(x.key===ASSIGNEE)o.selected=true;rev.appendChild(o);});
  }
  async function loadQueue(){const params=filterParams(); params.set('page',String(PAGE)); params.set('page_size','15'); params.set('group_by',GROUP);
    try{const d=await j('/api/queue?'+params.toString()); QUEUE.length=0; (d.items||[]).forEach(it=>QUEUE.push(it));
      PAGE=d.page; PAGES=d.pages; TOTAL=d.total; fillFacets(d.facets||{});
      const box=$('kb-queue'); box.innerHTML=''; let last=null;
      QUEUE.forEach(it=>{
        if(it.group && it.group!==last){const h=document.createElement('div');h.className='kb-group';h.textContent=it.group;box.appendChild(h);last=it.group;}
        const row=document.createElement('div'); row.className='kb-row';
        const cb=document.createElement('input'); cb.type='checkbox'; cb.checked=SEL_MAP.has(key(it));
        cb.onchange=()=>{if(cb.checked)SEL_MAP.set(key(it),it);else SEL_MAP.delete(key(it));updateSelCount();refreshImpact();};
        row.appendChild(cb);
        const text=document.createElement('span');
        text.innerHTML='<b>'+esc(it.intervention)+' '+esc(it.attribute)+'</b> '+esc(it.value)+'<br>'+esc(it.population)+' '+esc(it.severity)+(it.assignee?' · 审核人 '+esc(it.assignee):' · 未分配')+(it.sla?(it.sla.overdue?' · <b style="color:#a3443d">超时 '+Math.abs(it.sla.hours_left)+'h</b>':(it.sla.due_soon?' · <b style="color:#a56713">剩余 '+it.sla.hours_left+'h</b>':'')):'') ;
        row.appendChild(text); box.appendChild(row);});
      if(!QUEUE.length){box.textContent='队列为空';}
      $('kb-pageinfo').textContent='第 '+PAGE+'/'+PAGES+' 页 · 共 '+TOTAL+' 条';
      $('kb-page').textContent=PAGE+' / '+PAGES;
      $('kb-prev').disabled=PAGE<=1; $('kb-next').disabled=PAGE>=PAGES;}
    catch(e){$('kb-queue').textContent='队列加载失败：'+e.message;}}
  async function refreshImpact(){if(!SEL_MAP.size){$('kb-impact').style.display='none';return;}
    try{const all=selectedItems(); const totals={total:0,cache_entries:0,answer_log:0}; const changes={}; const hist=[];
      for(let i=0;i<all.length;i+=200){const items=all.slice(i,i+200).map(it=>({claim_id:it.claim_id,version_id:it.version_id,action:'approve'}));
        const d=await j('/api/batch/impact',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:items})});
        totals.total+=d.summary.total; totals.cache_entries=Math.max(totals.cache_entries,d.summary.cache_entries||0);
        totals.answer_log+=(d.summary.answer_log_affected||0);
        (d.items||[]).forEach(it=>{const al=it.answer_log||{}; (al.items||[]).forEach(h=>hist.push(h));});
        Object.entries(d.summary.changes||{}).forEach(([k,v])=>{changes[k]=(changes[k]||0)+v;});}
      const parts=Object.entries(changes).map(([k,v])=>(KIND_LABELS[k]||k)+' '+v);
      HISTORY=hist; renderHistory();
      $('kb-impact').style.display='block';
      $('kb-impact').textContent='影响预览：选中 '+totals.total+' 条；'+(parts.join('，')||'无变化')+'；检索缓存将失效 '+totals.cache_entries+' 条；历史答案受影响 '+totals.answer_log+' 条';}
    catch(e){$('kb-impact').style.display='block';$('kb-impact').textContent='影响预览失败：'+e.message;}}
  async function batch(action){if(!SEL_MAP.size){alert('请先勾选待审条目');return;}
    const all=selectedItems(); const reason=$('kb-reason').value; const note=$('kb-note').value.trim();
    const reasonText=reason?$('kb-reason').selectedOptions[0].textContent:'无';
    if(!confirm('确认对 '+all.length+' 条执行「'+(action==='approve'?'通过':'拒绝')+'」？\\n理由：'+reasonText+(note?'（'+note+'）':''))) return;
    let ok=0, fail=0;
    try{for(let i=0;i<all.length;i+=50){const items=all.slice(i,i+50).map(it=>({claim_id:it.claim_id,version_id:it.version_id,action:action,reason:reason||'other',note:note}));
        const d=await j('/api/batch/decide',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:items})});
        ok+=d.succeeded; fail+=d.failed;}
      $('kb-impact').style.display='block'; $('kb-impact').textContent='批量完成：成功 '+ok+' / 失败 '+fail;
      SEL_MAP.clear(); updateSelCount(); loadQueue(); loadTodos(); loadSla();}
    catch(e){$('kb-impact').style.display='block';$('kb-impact').textContent='批量失败：'+e.message;}}
  async function selectAllFiltered(){try{const d=await j('/api/queue/keys?'+filterParams().toString());
      (d.items||[]).forEach(it=>SEL_MAP.set(key(it),it)); updateSelCount(); refreshImpact();}
    catch(e){$('kb-impact').style.display='block';$('kb-impact').textContent='全选失败：'+e.message;}}
  $('kb-ba').onclick=()=>batch('approve');
  $('kb-br').onclick=()=>batch('reject');
  $('kb-preview').onclick=refreshImpact;
  $('kb-selectall').onclick=selectAllFiltered;
  $('kb-clear').onclick=()=>{SEL_MAP.clear();HISTORY=[];renderHistory();updateSelCount();refreshImpact();};
  $('kb-csv').onclick=()=>{location.href='/api/export/queue.csv?'+filterParams().toString();};
  $('kb-regen-all').onclick=regenerateAll;
  async function assignSelected(){const reviewer=$('kb-assignee').value.trim();
    if(!reviewer){alert('请输入审核人');return;} if(!SEL_MAP.size){alert('请先勾选待审条目');return;}
    const items=selectedItems().map(it=>({claim_id:it.claim_id,version_id:it.version_id}));
    try{const d=await j('/api/assign',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:items,reviewer:reviewer})});
      $('kb-impact').style.display='block'; $('kb-impact').textContent='分配完成：成功 '+d.succeeded+' / 失败 '+d.failed+' · 审核人 '+reviewer;
      loadQueue(); loadSla();}
    catch(e){$('kb-impact').style.display='block';$('kb-impact').textContent='分配失败：'+e.message;}}
  async function loadLedger(){try{const d=await j('/api/reviews?limit=30'); const pre=$('kb-ledger'); pre.style.display='block';
      const lines=(d.items||[]).map(it=>[it.decided_at||'',it.decision||'',it.intervention||'',it.attribute||'',it.value||'',
        '审核人='+(it.reviewer||'-'),'分配='+(it.assigned_reviewer||'-'),'理由='+(it.reason_code||'-'),'备注='+(it.review_note||'-')].join(' '));
      pre.textContent=lines.join('\\n')||'暂无审核记录';}
    catch(e){$('kb-ledger').style.display='block';$('kb-ledger').textContent='台账加载失败：'+e.message;}}
  $('kb-assign').onclick=assignSelected;
  $('kb-ledger-btn').onclick=loadLedger;
  $('kb-export-reviews').onclick=()=>{location.href='/api/export/reviews.csv';};
  $('kb-revfilter').onchange=()=>{ASSIGNEE=$('kb-revfilter').value; PAGE=1; loadQueue();};
  $('kb-sla').onchange=()=>{SLA=$('kb-sla').value; PAGE=1; loadQueue();};
  $('kb-notify').onclick=async()=>{try{const d=await j('/api/sla/notify',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
      $('kb-impact').style.display='block'; $('kb-impact').textContent='SLA 通知已发送：通道='+(d.channels||[]).join(',')+' · 超时 '+d.overdue+' / 到期 '+d.due_soon;}
    catch(e){$('kb-impact').style.display='block';$('kb-impact').textContent='SLA 通知失败：'+e.message;}};
  $('kb-group').onchange=()=>{GROUP=$('kb-group').value; PAGE=1; loadQueue();};
  $('kb-drug').onchange=()=>{DRUG=$('kb-drug').value; PAGE=1; loadQueue();};
  $('kb-sev').onchange=()=>{SEV=$('kb-sev').value; PAGE=1; loadQueue();};
  $('kb-apply').onclick=()=>{Q=$('kb-q').value.trim(); PAGE=1; loadQueue();};
  $('kb-prev').onclick=()=>{if(PAGE>1){PAGE-=1;loadQueue();}};
  $('kb-next').onclick=()=>{if(PAGE<PAGES){PAGE+=1;loadQueue();}};
  async function runCase(caseObj){
    $('kb-result').textContent='分析中，请稍候…';
    try{
      const d=await j('/api/case/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({case:caseObj})});
      const label=d.interrupted?'需要人工确认（高风险）':'已完成';
      const cx=d.complexity==='complex'?'复杂问题':'简单问题';
      let text='结果：'+label+'\\n判断：'+cx;
      if(d.route_reason&&d.route_reason.length){text+='\\n依据：'+d.route_reason.join('、');}
      if(d.interrupt&&d.interrupt.length){text+='\\n原因：'+((d.interrupt[0]||{}).reason||'high_risk');}
      if(d.answer){text+='\\n\\n'+d.answer;}
      $('kb-result').textContent=text;
    }catch(e){$('kb-result').textContent='运行失败：'+e.message;}
  }
  $('kb-case-simple').onclick=()=>{
    $('kb-case-question').value='成人 阿奇霉素 用法';
    $('kb-case-age').value='30';
    $('kb-case-drug').value='阿奇霉素';
    $('kb-case-severity').value='';
    runForm();
  };
  $('kb-case-risk').onclick=()=>{
    $('kb-case-question').value='儿童 重症 难治性 阿奇霉素 剂量调整';
    $('kb-case-age').value='5';
    $('kb-case-drug').value='阿奇霉素';
    $('kb-case-severity').value='severe';
    runForm();
  };
  function runForm(){
    const age=Number($('kb-case-age').value);
    const context={drug:$('kb-case-drug').value.trim()};
    if(Number.isFinite(age)) context.age_years=age;
    const severity=$('kb-case-severity').value;
    if(severity) context.severity=severity;
    runCase({case_id:'ui-case-'+Date.now(),question:$('kb-case-question').value.trim(),patient_context:context});
  }
  $('kb-run').onclick=runForm;
  function renderEvidenceStatusDetails(data){
    const block=data&&data.evidence_status_block;
    const answer=document.getElementById('final-answer');
    if(!answer||!block||!block.summary) return;
    let details=document.getElementById('kb-evidence-status');
    if(!details){
      details=document.createElement('details');
      details.id='kb-evidence-status';
      answer.insertAdjacentElement('afterend',details);
    }
    details.replaceChildren();
    const summary=document.createElement('summary');
    summary.textContent='证据状态详情';
    details.appendChild(summary);
    const body=document.createElement('div');
    const addRow=(label,value)=>{
      const row=document.createElement('div');
      row.className='kb-status-row';
      const l=document.createElement('span'); l.className='kb-status-label'; l.textContent=label;
      const v=document.createElement('span'); v.className='kb-status-value'; v.textContent=value==null?'-':String(value);
      row.append(l,v); body.appendChild(row);
    };
    addRow('知识库版本','v'+(block.library_version==null?'?':block.library_version));
    addRow('快照',block.snapshot_label||block.snapshot_id||'未记录');
    const mode=block.retrieval_mode==='hybrid'?'混合检索':block.retrieval_mode==='lexical'?'词法检索':(block.retrieval_mode||'未记录');
    addRow('检索模式',mode);
    addRow('候选证据',block.candidate_claims==null?'-':block.candidate_claims+' 条');
    addRow('热库待审',(block.hot_count||0)+' 条');
    addRow('冲突',(block.conflict_count||0)+' 处');
    const conflicts=(block.conflicts||[]).map(item=>{
      const drug=item.intervention_label||item.intervention||'';
      const attr=item.attribute_label||item.attribute||'';
      return `${drug} · ${attr}：${item.value_a} vs ${item.value_b}`;
    });
    if(conflicts.length) addRow('冲突明细',conflicts.join('；'));
    const gaps=block.evidence_gaps||[];
    addRow('证据缺口',gaps.length?gaps.join('；'):'无');
    const gapDetails=block.gap_details||[];
    if(gapDetails.length){
      addRow('缺口建议',gapDetails.map(item=>`${item.drug}：${item.suggestion||''}`).join('；'));
    }
    const citation=block.citation_check||{};
    addRow('引用核查',citation.sentences?`${Math.round((citation.coverage||0)*100)}%（${citation.cited||0}/${citation.sentences}）`:'未检测');
    if(citation.unsupported&&citation.unsupported.length){
      addRow('未附来源句',citation.unsupported.slice(0,5).join('；'));
    }
    const fills=block.gap_fills||[];
    if(fills.length){
      addRow('自动补检索',fills.map(item=>`${item.drug}：${item.message}`).join('；'));
    }
    const hints=[];
    fills.forEach(item=>(item.source_hints||[]).forEach(hint=>hints.push(hint)));
    if(hints.length){
      const row=document.createElement('div');
      row.className='kb-status-row';
      const label=document.createElement('span'); label.className='kb-status-label'; label.textContent='原文详情';
      const value=document.createElement('span'); value.className='kb-status-value';
      hints.slice(0,5).forEach((hint,index)=>{
        const button=document.createElement('button');
        button.className='kb-src-detail';
        button.style.margin='0 6px 4px 0';
        button.textContent='详情 '+(index+1);
        const meta=`${hint.title||hint.doc_id||'原文'} · ${hint.year||'年份未知'} · 第 ${hint.page==null?'?':hint.page} 页`;
        button.onclick=()=>openSourceModal('原文线索',meta,hint.quote_full||hint.quote||'');
        value.appendChild(button);
      });
      row.append(label,value); body.appendChild(row);
    }
    details.appendChild(body);
  }
  function setupGraphHover(){
    const graph=document.getElementById('evidence-graph');
    if(!graph||graph.dataset.kbHover) return;
    graph.dataset.kbHover='1';
    const clear=()=>{
      graph.querySelectorAll('.graph-edge').forEach(edge=>{
        edge.classList.remove('kb-edge-active','kb-edge-dim');
      });
    };
    graph.addEventListener('mouseover',event=>{
      const node=event.target.closest&&event.target.closest('.graph-node');
      if(!node) return;
      const id=node.getAttribute('data-node-id');
      if(!id) return;
      graph.querySelectorAll('.graph-edge').forEach(edge=>{
        const connected=edge.getAttribute('data-source')===id||edge.getAttribute('data-target')===id;
        edge.classList.toggle('kb-edge-active',connected);
        edge.classList.toggle('kb-edge-dim',!connected);
      });
    });
    graph.addEventListener('mouseleave',clear);
    graph.addEventListener('mouseout',event=>{
      if(!event.relatedTarget||!graph.contains(event.relatedTarget)) clear();
    });
  }
  function setupGraphGate(){
    const graph=document.getElementById('evidence-graph');
    if(!graph||graph.dataset.kbGate) return;
    graph.dataset.kbGate='1';
    graph.style.display='none';
    window.__KB_SHOW_GRAPH=function(){
      const currentGraph=document.getElementById('evidence-graph');
      if(currentGraph) currentGraph.style.display='';
    };
  }
  setupGraphHover();
  setupGraphGate();
  loadTodos(); loadReasons(); updateSelCount(); loadSla();
  if(location.hash==='#kb'){$('kb-panel').style.display='block';loadQueue();}
})();
</script>
"""


def _value_text(value: dict[str, Any], direction: str | None) -> str:
    kind = value.get("kind")
    if kind == "numeric":
        return f"{value.get('value')} {value.get('unit') or ''}".strip()
    if kind == "range":
        return f"{value.get('low')}–{value.get('high')} {value.get('unit') or ''}".strip()
    if kind == "direction":
        labels = {"first_line": "首选", "alternative": "备选", "avoid": "避免"}
        return labels.get(str(value.get("value") or direction), str(value.get("value") or direction))
    if kind == "text":
        return str(value.get("value") or "")
    return json.dumps(value, ensure_ascii=False)


class KernelBridge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.kb, self.corpus = ensure_databases(cfg)
        self.service = EvidenceService(self.kb, cfg)
        self._catalog: dict[str, Any] | None = None

    def _papers(self) -> dict[str, dict[str, Any]]:
        rows = self.corpus.execute(
            "SELECT paper_id, title, year, publisher, evidence_type FROM papers"
        ).fetchall()
        return {row["paper_id"]: dict(row) for row in rows}

    def _catalog_nodes(self) -> list[dict[str, Any]]:
        rows = self.kb.execute(
            "SELECT v.version_id, v.claim_id, v.value_json, v.direction, v.evidence_type, "
            "v.provenance, v.anchors_json, v.published_date, "
            "c.population, c.severity, c.intervention, c.attribute "
            "FROM claims c JOIN claim_versions v ON v.claim_id = c.claim_id "
            "WHERE v.status = 'approved' "
            "ORDER BY c.intervention, c.attribute LIMIT 500"
        ).fetchall()
        papers = self._papers()
        nodes: list[dict[str, Any]] = []
        for row in rows:
            value = json.loads(row["value_json"])
            anchors = json.loads(row["anchors_json"] or "[]")
            anchor = anchors[0] if anchors else {}
            doc_id = str(anchor.get("doc_id") or "")
            paper = papers.get(doc_id) or {}
            evidence_type = row["evidence_type"] or paper.get("evidence_type")
            if evidence_type not in ALLOWED_TYPES:
                evidence_type = "guideline"
            year = paper.get("year")
            if not year and row["published_date"]:
                try:
                    year = int(str(row["published_date"])[:4])
                except ValueError:
                    year = None
            label = " · ".join(
                part
                for part in (
                    self.service._label(row["population"]),
                    self.service._label(row["severity"]),
                    self.service._label(row["intervention"]),
                    self.service._label(row["attribute"]),
                )
                if part
            )
            text = f"{label}：{_value_text(value, row['direction'])}"
            citation = (
                f"{doc_id} p.{anchor.get('page_start')}"
                if doc_id and anchor.get("page_start")
                else str(row["version_id"])
            )
            nodes.append(
                {
                    "id": row["version_id"],
                    "claim_id": row["claim_id"],
                    "title": text,
                    "evidence_type": evidence_type,
                    "year": year or 2024,
                    "month": 1,
                    "source": paper.get("publisher") or paper.get("title") or "mppkb 冷库（受治理）",
                    "population": self.service._label(row["population"]),
                    "intervention": self.service._label(row["intervention"]),
                    "comparator": "",
                    "main_findings": text,
                    "quality": "high",
                    "recommendation_direction": "support",
                    "dose_signal": "not_specific",
                    "safety_signals": (
                        ["P0 安全例外"] if row["provenance"] == "p0_exception" else []
                    ),
                    "tags": [
                        TYPE_LABELS.get(evidence_type, evidence_type),
                        self.service._label(row["intervention"]),
                    ],
                    "citation": citation,
                    "_group": (
                        row["intervention"],
                        row["attribute"],
                    ),
                    "_value": _value_text(value, row["direction"]),
                    "_year": year or 2024,
                    "_p0": row["provenance"] == "p0_exception",
                }
            )
        return nodes

    def _build_edges(self, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[tuple, list[dict[str, Any]]] = {}
        for node in nodes:
            groups.setdefault(node["_group"], []).append(node)
        edges: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()

        def add(source: dict[str, Any], target: dict[str, Any], relation: str, description: str) -> None:
            key = (source["id"], target["id"], relation)
            if key in seen or source["id"] == target["id"]:
                return
            seen.add(key)
            edges.append(
                {
                    "source": source["id"],
                    "target": target["id"],
                    "relation": relation,
                    "description": description,
                }
            )

        for group in groups.values():
            if len(group) < 2:
                continue
            ordered = sorted(group, key=lambda item: (item["_year"], item["id"]))
            for index, older in enumerate(ordered):
                for newer in ordered[index + 1 :]:
                    if newer["_value"] == older["_value"]:
                        add(newer, older, "supports", "同一结论的多条证据一致")
                    elif newer["_year"] != older["_year"]:
                        add(newer, older, "updates", "较新证据更新较早结论")
                    else:
                        add(newer, older, "cautions", "同一槽位数值不一致，需人工裁决")
            p0_nodes = [node for node in group if node["_p0"]]
            for p0_node in p0_nodes:
                for other in group:
                    if not other["_p0"]:
                        add(p0_node, other, "cautions", "P0 安全例外覆盖该推荐")
        return edges[:120]

    @staticmethod
    def _public(node: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in node.items() if not key.startswith("_")}

    def catalog(self) -> dict[str, Any]:
        if self._catalog is None:
            nodes = self._catalog_nodes()
            edges = self._build_edges(nodes)
            counts = {evidence_type: 0 for evidence_type in ALLOWED_TYPES}
            for node in nodes:
                counts[node["evidence_type"]] += 1
            self._catalog = {
                "default_question": DEFAULT_QUESTION,
                "nodes": [self._public(node) for node in nodes],
                "edges": edges,
                "evidence_types": [
                    {
                        "key": evidence_type,
                        "label": TYPE_LABELS[evidence_type],
                        "count": counts[evidence_type],
                    }
                    for evidence_type in ALLOWED_TYPES
                ],
            }
        return self._catalog

    def _node_for_claim(
        self, claim: dict[str, Any], catalog: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        version_id = claim.get("version_id")
        if version_id in catalog:
            return catalog[version_id]
        population = self.service._label(claim.get("population"))
        severity = self.service._label(claim.get("severity"))
        intervention = self.service._label(claim.get("intervention"))
        attribute = self.service._label(claim.get("attribute"))
        label = " · ".join(
            part for part in (population, severity, intervention, attribute) if part
        )
        text = (
            f"{label}：{_value_text(claim.get('value') or {}, claim.get('direction'))}"
            if label
            else _value_text(claim.get("value") or {}, claim.get("direction"))
        )
        evidence_type = str(claim.get("evidence_type") or "guideline")
        if evidence_type not in ALLOWED_TYPES:
            evidence_type = "guideline"
        return {
            "id": version_id,
            "claim_id": claim.get("claim_id"),
            "title": text,
            "evidence_type": evidence_type,
            "year": 2024,
            "month": 1,
            "source": "mppkb 冷库（受治理）",
            "population": population,
            "severity": severity,
            "intervention": intervention,
            "comparator": "",
            "main_findings": text,
            "quality": "high",
            "recommendation_direction": "support",
            "dose_signal": "not_specific",
            "safety_signals": [],
            "tags": [TYPE_LABELS.get(evidence_type, evidence_type), intervention],
            "citation": str(version_id),
            "_group": (claim.get("intervention"), claim.get("attribute")),
            "_value": _value_text(claim.get("value") or {}, claim.get("direction")),
            "_year": 2024,
            "_p0": False,
        }

    def analyze(self, question: str) -> dict[str, Any]:
        question = str(question or "").strip() or DEFAULT_QUESTION
        result = answer(self.kb, self.cfg, question, corpus=self.corpus)
        catalog = {node["id"]: node for node in self.catalog()["nodes"]}
        claims = result.get("claims") or []
        nodes_by_claim: dict[str, dict[str, Any]] = {}
        for claim in claims:
            node = self._node_for_claim(claim, catalog)
            nodes_by_claim.setdefault(str(claim.get("claim_id") or node["id"]), node)
        nodes = list(nodes_by_claim.values())
        node_ids = {node["id"] for node in nodes}
        edges = [
            edge
            for edge in self.catalog()["edges"]
            if edge["source"] in node_ids and edge["target"] in node_ids
        ]
        update_count = sum(1 for edge in edges if edge["relation"] == "updates")
        caution_count = sum(1 for edge in edges if edge["relation"] == "cautions")
        p0 = result.get("p0_warning")
        sections = result.get("sections") or []

        markdown_parts: list[str] = []
        conclusion = str(result.get("conclusion") or "").strip()
        if conclusion:
            markdown_parts.append(f"**结论**\n{conclusion}")
        if p0:
            markdown_parts.append(f"**【安全警示】** {p0.get('template')}（P0 安全例外）")
        status_block = result.get("evidence_status_block") or {}
        if status_block.get("summary"):
            markdown_parts.append(f"**证据状态**：{status_block['summary']}")
        for section in sections:
            markdown_parts.append(f"**{section.get('name')}**：{section.get('text')}")
        treatment_elements = result.get("treatment_elements") or []
        if treatment_elements:
            plan_lines = ["**治疗要素（自动汇总，非处方）**"]
            for element in treatment_elements:
                plan_lines.append(f"**{element.get('drug')}**")
                for scenario in element.get("scenarios") or []:
                    parts = []
                    for group, items in (scenario.get("groups") or {}).items():
                        values = []
                        for item in items:
                            value = item.get("value") or item.get("text") or ""
                            values.append(f"{value}[Source[{item.get('source_no')}]]")
                        if values:
                            parts.append(f"{group}：{' / '.join(values)}")
                    if parts:
                        plan_lines.append(f"- **{scenario.get('context')}**：" + "；".join(parts))
                missing = element.get("missing") or []
                if missing:
                    plan_lines.append("- 未收录：" + "、".join(missing))
            plan_lines.append("以上为已审核证据的要素汇总，不构成处方；缺失项需查原文或由临床判断。")
            markdown_parts.append("\n".join(plan_lines))
        gap_details = result.get("evidence_gap_details") or []
        if gap_details:
            gap_lines = ["**证据缺口（需查原文或补充数据）**"]
            for detail in gap_details[:6]:
                gap_lines.append(
                    f"- {detail.get('drug')}：缺少「{'、'.join(detail.get('missing') or [])}」；"
                    f"建议补查：{detail.get('suggestion') or '查原文'}"
                )
            markdown_parts.append("\n".join(gap_lines))
        gap_fills = result.get("gap_fills") or []
        if gap_fills:
            fill_lines = ["**自动补检索（仅供核对，未进入结论）**"]
            for fill in gap_fills:
                if fill.get("action") == "hot_pending":
                    fill_lines.append(f"- {fill.get('drug')}：{fill.get('message')}")
                elif fill.get("action") == "source_hint":
                    hints = fill.get("source_hints") or []
                    first = hints[0] if hints else {}
                    fill_lines.append(
                        f"- {fill.get('drug')}：{fill.get('message')}；"
                        f"线索：{first.get('title') or first.get('doc_id')} "
                        f"（{first.get('year') or '年份未知'}，第 {first.get('page') or '?'} 页）"
                    )
                else:
                    fill_lines.append(f"- {fill.get('drug')}：{fill.get('message')}")
            markdown_parts.append("\n".join(fill_lines))
        citation_check = result.get("citation_check") or {}
        if citation_check.get("defects"):
            markdown_parts.append(
                f"**引用核查**：{citation_check.get('defects')} 句未附来源，需人工核对。"
            )
        source_details = result.get("source_details") or []
        if source_details:
            markdown_parts.append(
                "> 点击正文中的 [Source[n]] 可查看对应的文档、页码和原文。"
            )
        if result.get("message"):
            markdown_parts.append(str(result["message"]))
        markdown_parts.append(f"> {result.get('disclaimer') or ''}")
        answer_markdown = "\n\n".join(part for part in markdown_parts if part)

        if sections:
            conclusion = conclusion or sections[0]["text"][:80]
        elif p0:
            conclusion = conclusion or "命中 P0 安全例外，推荐被压制。"
        else:
            conclusion = conclusion or "没有足够的当前证据回答该问题。"
        if caution_count:
            consistency = f"存在 {caution_count} 条警示关系"
        elif update_count:
            consistency = f"存在 {update_count} 条更新关系"
        elif nodes:
            consistency = "基本一致"
        else:
            consistency = "证据不足"
        reasoning_steps = [
            {
                "title": "1. 问题解析",
                "body": f"问题：{question}。路由：内核 EvidenceService 直接进入冷库检索。",
                "node_ids": [],
            },
            {
                "title": "2. 冷库检索",
                "body": f"命中 {len(claims)} 条 approved claim（模式：{result.get('retrieval', {}).get('mode', 'lexical')}）。",
                "node_ids": list(node_ids)[:8],
            },
            {
                "title": "3. 版本与关系检查",
                "body": (
                    f"只使用当前冷库版本；本次可见 {len(edges)} 条证据关系"
                    f"（更新 {update_count} / 警示 {caution_count}）。"
                ),
                "node_ids": list(node_ids)[:8],
            },
            {
                "title": "4. P0 安全检查",
                "body": (p0.get("template") if p0 else "未命中 P0 安全例外。"),
                "node_ids": [],
            },
            {
                "title": "5. 引用与归因核查",
                "body": (
                    f"{len(result.get('citations') or [])} 条页码引用；"
                    f"{len(result.get('attribution') or [])} 句归因。Source 标记由代码生成。"
                ),
                "node_ids": [],
            },
            {
                "title": "6. 受约束合成",
                "body": "答案只由锁定 claim 生成；热库证据不进入回答正文。",
                "node_ids": [],
            },
        ]
        return {
            "question": question,
            "evidence": nodes,
            "edges": edges,
            "reasoning_steps": reasoning_steps,
            "treatment_elements": result.get("treatment_elements") or [],
            "source_details": result.get("source_details") or [],
            "evidence_status_block": result.get("evidence_status_block") or {},
            "library_version": result.get("library_version"),
            "snapshot_id": result.get("snapshot_id"),
            "routing": result.get("routing") or {},
            "budgets": result.get("budgets") or {},
            "orchestration_v2": bool(result.get("orchestration_v2")),
            "planned_expansion": bool(result.get("planned_expansion")),
            "requested_groups": result.get("requested_groups") or [],
            "conflicts": result.get("conflicts") or [],
            "evidence_gaps": result.get("evidence_gaps") or [],
            "evidence_gap_details": result.get("evidence_gap_details") or [],
            "gap_fills": result.get("gap_fills") or [],
            "citation_check": result.get("citation_check") or {},
            "summary": {
                "evidence_count": len(nodes),
                "consistency": consistency,
                "update_count": update_count,
                "conclusion": conclusion,
            },
            "answer_markdown": answer_markdown,
            "model_name": "mppkb-kernel / A+C",
            "model_used": True,
        }

    def _queue_all(self) -> list[dict[str, Any]]:
        assignments = list_assignments(self.kb)
        items = []
        for item in list_queue(self.kb):
            slot = item.get("slot") or {}
            if isinstance(slot, str):
                parts = slot.split("|")
                population, severity, intervention, attribute = (parts + [""] * 4)[:4]
            else:
                population = slot.get("population")
                severity = slot.get("severity")
                intervention = slot.get("intervention")
                attribute = slot.get("attribute")
            value = item.get("value") or {}
            assignment = assignments.get(item.get("version_id")) or {}
            sla = assignment_sla(assignment["assigned_at"]) if assignment else None
            sla_status = ""
            if sla is not None:
                sla_status = (
                    "overdue" if sla["overdue"] else "due_soon" if sla["due_soon"] else "on_time"
                )
            items.append(
                {
                    "claim_id": item.get("claim_id"),
                    "version_id": item.get("version_id"),
                    "status": item.get("status"),
                    "provenance": item.get("provenance"),
                    "evidence_type": item.get("evidence_type") or "",
                    "population": self.service._label(population),
                    "severity": self.service._label(severity),
                    "intervention": self.service._label(intervention),
                    "attribute": self.service._label(attribute),
                    "value": _value_text(value, item.get("direction")),
                    "intervention_key": intervention or "",
                    "severity_key": severity or "",
                    "assignee": assignment.get("reviewer") or "",
                    "assigned_at": assignment.get("assigned_at") or "",
                    "sla": sla,
                    "sla_status": sla_status,
                }
            )
        return items

    def queue(self) -> dict[str, Any]:
        return {"ok": True, "items": self._queue_all()[:20]}

    def _filter_items(
        self,
        items: list[dict[str, Any]],
        intervention: str = "",
        severity: str = "",
        q: str = "",
        assignee: str = "",
        sla: str = "",
    ) -> list[dict[str, Any]]:
        filtered = items
        if intervention:
            filtered = [item for item in filtered if item["intervention_key"] == intervention]
        if severity:
            filtered = [item for item in filtered if item["severity_key"] == severity]
        if assignee == "__unassigned":
            filtered = [item for item in filtered if not item.get("assignee")]
        elif assignee:
            filtered = [item for item in filtered if item.get("assignee") == assignee]
        if sla in ("overdue", "due_soon", "on_time"):
            filtered = [item for item in filtered if item.get("sla_status") == sla]
        needle = str(q or "").strip().lower()
        if needle:
            filtered = [
                item
                for item in filtered
                if needle
                in " ".join(
                    str(item.get(field) or "")
                    for field in (
                        "intervention",
                        "severity",
                        "attribute",
                        "value",
                        "claim_id",
                        "evidence_type",
                    )
                ).lower()
            ]
        return filtered

    def queue_keys(
        self,
        intervention: str = "",
        severity: str = "",
        q: str = "",
        assignee: str = "",
        sla: str = "",
    ) -> dict[str, Any]:
        items = self._filter_items(
            self._queue_all(),
            intervention=intervention,
            severity=severity,
            q=q,
            assignee=assignee,
            sla=sla,
        )
        return {
            "ok": True,
            "total": len(items),
            "items": [
                {
                    "claim_id": item["claim_id"],
                    "version_id": item["version_id"],
                    "status": item["status"],
                    "provenance": item["provenance"],
                    "intervention": item["intervention"],
                    "severity": item["severity"],
                    "attribute": item["attribute"],
                    "value": item["value"],
                }
                for item in items
            ],
        }

    def queue_csv(
        self,
        intervention: str = "",
        severity: str = "",
        q: str = "",
        assignee: str = "",
        sla: str = "",
    ) -> str:
        items = self._filter_items(
            self._queue_all(),
            intervention=intervention,
            severity=severity,
            q=q,
            assignee=assignee,
            sla=sla,
        )
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "claim_id",
                "version_id",
                "status",
                "provenance",
                "evidence_type",
                "population",
                "severity",
                "intervention",
                "attribute",
                "value",
                "assignee",
                "sla_status",
                "due_at",
            ]
        )
        for item in items:
            writer.writerow(
                [
                    item.get("claim_id"),
                    item.get("version_id"),
                    item.get("status"),
                    item.get("provenance"),
                    item.get("evidence_type"),
                    item.get("population"),
                    item.get("severity"),
                    item.get("intervention"),
                    item.get("attribute"),
                    item.get("value"),
                    item.get("assignee"),
                    item.get("sla_status"),
                    (item.get("sla") or {}).get("due_at"),
                ]
            )
        return "\ufeff" + output.getvalue()

    def reason_codes(self) -> dict[str, Any]:
        return {
            "ok": True,
            "items": [
                {"code": code, "label": REASON_LABELS.get(code, code)}
                for code in REASON_CODES
            ],
        }

    def queue_page(
        self,
        page: int = 1,
        page_size: int = 20,
        group_by: str = "intervention",
        intervention: str = "",
        severity: str = "",
        q: str = "",
        assignee: str = "",
        sla: str = "",
    ) -> dict[str, Any]:
        """Filtered, faceted and paginated review queue for large batches."""
        items = self._queue_all()
        interventions: dict[str, dict[str, Any]] = {}
        severities: dict[str, dict[str, Any]] = {}
        reviewers_facet: dict[str, dict[str, Any]] = {}
        sla_facet = {"overdue": 0, "due_soon": 0, "on_time": 0, "unassigned": 0}
        for item in items:
            for key, facet in (
                (item["intervention_key"], interventions),
                (item["severity_key"], severities),
            ):
                if not key:
                    continue
                bucket = facet.setdefault(key, {"key": key, "label": "", "count": 0})
                bucket["label"] = (
                    item["intervention"] if key == item["intervention_key"] else item["severity"]
                )
                bucket["count"] += 1
            reviewer_key = item.get("assignee") or "__unassigned"
            reviewer = reviewers_facet.setdefault(
                reviewer_key,
                {
                    "key": reviewer_key,
                    "label": item.get("assignee") or "未分配",
                    "count": 0,
                },
            )
            reviewer["count"] += 1
            sla_facet[item.get("sla_status") or "unassigned"] += 1
        filtered = self._filter_items(
            items,
            intervention=intervention,
            severity=severity,
            q=q,
            assignee=assignee,
            sla=sla,
        )
        group_by = group_by if group_by in ("intervention", "severity", "none") else "intervention"
        if group_by != "none":
            field = "intervention" if group_by == "intervention" else "severity"
            filtered = sorted(
                filtered,
                key=lambda item: (
                    item.get(field) or "",
                    item.get("attribute") or "",
                    item.get("value") or "",
                    item.get("claim_id") or "",
                ),
            )
            for item in filtered:
                item["group"] = item.get(field) or "未标注"
        else:
            for item in filtered:
                item["group"] = ""
        total = len(filtered)
        page_size = max(1, min(int(page_size or 20), 100))
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(int(page or 1), pages))
        start = (page - 1) * page_size
        page_items = filtered[start : start + page_size]
        return {
            "ok": True,
            "page": page,
            "page_size": page_size,
            "pages": pages,
            "total": total,
            "group_by": group_by,
            "items": page_items,
            "facets": {
                "interventions": sorted(
                    interventions.values(), key=lambda item: (-item["count"], item["label"])
                ),
                "severities": sorted(
                    severities.values(), key=lambda item: (-item["count"], item["label"])
                ),
                "reviewers": sorted(
                    reviewers_facet.values(),
                    key=lambda item: (-item["count"], item["label"]),
                ),
                "sla": sla_facet,
            },
        }

    def assign(
        self, items: list[dict[str, Any]], reviewer: str, assigned_by: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(items, list) or not items:
            raise ValueError("items must be a non-empty array")
        if len(items) > 500:
            raise ValueError("at most 500 items per assignment")
        return assign_batch(self.kb, items, reviewer, assigned_by)

    def reviewers(self) -> dict[str, Any]:
        return {"ok": True, "items": list_reviewers(self.kb)}

    def sla(self) -> dict[str, Any]:
        items = [item for item in self._queue_all() if item.get("assignee")]
        overdue = [item for item in items if item.get("sla_status") == "overdue"]
        due_soon = [item for item in items if item.get("sla_status") == "due_soon"]
        return {
            "ok": True,
            "hours": review_sla_hours(),
            "warn_hours": review_sla_warn_hours(),
            "overdue": len(overdue),
            "due_soon": len(due_soon),
            "items": (overdue + due_soon)[:20],
        }

    def notify_sla(self) -> dict[str, Any]:
        summary = self.sla()
        lines = [
            f"MPPKB 审核 SLA 提醒：超时 {summary['overdue']} 条，即将到期 {summary['due_soon']} 条。",
        ]
        for item in summary["items"]:
            hours = (item.get("sla") or {}).get("hours_left")
            lines.append(
                f"- {item.get('intervention')} {item.get('attribute')} "
                f"{item.get('value')} · 审核人 {item.get('assignee')} · 剩余 {hours}h"
            )
        text = "\n".join(lines)
        channels: list[str] = []
        errors: list[str] = []
        webhook = os.environ.get("MPPKB_SLA_WEBHOOK_URL", "").strip()
        if webhook:
            try:
                payload = json.dumps(
                    {
                        "text": text,
                        "overdue": summary["overdue"],
                        "due_soon": summary["due_soon"],
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                request = urllib.request.Request(
                    webhook,
                    data=payload,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    method="POST",
                )
                urllib.request.urlopen(request, timeout=10).read()
                channels.append("webhook")
            except Exception as exc:  # noqa: BLE001 - notification best effort
                errors.append(f"webhook: {exc}")
        smtp_host = os.environ.get("MPPKB_SMTP_HOST", "").strip()
        recipients = [
            item.strip()
            for item in os.environ.get("MPPKB_SMTP_TO", "").split(",")
            if item.strip()
        ]
        if smtp_host and recipients:
            try:
                sender = os.environ.get("MPPKB_SMTP_FROM", "").strip() or os.environ.get(
                    "MPPKB_SMTP_USER", ""
                )
                message = MIMEText(text, "plain", "utf-8")
                message["Subject"] = "MPPKB 审核 SLA 提醒"
                message["From"] = sender
                message["To"] = ", ".join(recipients)
                port = int(os.environ.get("MPPKB_SMTP_PORT", "587"))
                with smtplib.SMTP(smtp_host, port, timeout=10) as client:
                    if os.environ.get("MPPKB_SMTP_TLS", "1") != "0":
                        client.starttls()
                    user = os.environ.get("MPPKB_SMTP_USER", "")
                    password = os.environ.get("MPPKB_SMTP_PASS", "")
                    if user and password:
                        client.login(user, password)
                    client.sendmail(sender, recipients, message.as_string())
                channels.append("email")
            except Exception as exc:  # noqa: BLE001 - notification best effort
                errors.append(f"smtp: {exc}")
        if not webhook and not (smtp_host and recipients):
            raise ValueError(
                "未配置通知通道：请设置 MPPKB_SLA_WEBHOOK_URL 或 MPPKB_SMTP_HOST/TO"
            )
        with self.kb:
            self.kb.execute(
                "INSERT INTO kb_audit(event_seq, action, actor, detail_json, created_at) "
                "VALUES (NULL, 'sla_notify', 'system', ?, datetime('now'))",
                (
                    json.dumps(
                        {
                            "overdue": summary["overdue"],
                            "due_soon": summary["due_soon"],
                            "channels": channels,
                            "errors": errors,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
        return {
            "ok": bool(channels),
            "channels": channels,
            "errors": errors,
            "overdue": summary["overdue"],
            "due_soon": summary["due_soon"],
            "text": text,
        }

    def ledger(self, limit: int = 50, reviewer: str = "", action: str = "") -> dict[str, Any]:
        items = []
        for item in review_ledger(self.kb, limit, reviewer, action):
            items.append(
                {
                    **{
                        key: item[key]
                        for key in (
                            "claim_id",
                            "version_id",
                            "decision",
                            "reviewer",
                            "assigned_reviewer",
                            "decided_at",
                            "reason_code",
                            "review_note",
                            "provenance",
                            "evidence_type",
                        )
                    },
                    "population": self.service._label(item.get("population")),
                    "severity": self.service._label(item.get("severity")),
                    "intervention": self.service._label(item.get("intervention")),
                    "attribute": self.service._label(item.get("attribute")),
                    "value": _value_text(item.get("value") or {}, None),
                }
            )
        return {"ok": True, "items": items}

    def reviews_export(self, reviewer: str = "", action: str = "") -> str:
        return reviews_csv(self.kb, reviewer, action)

    def regenerate(self, seq: int) -> dict[str, Any]:
        row = self.kb.execute(
            "SELECT seq, case_id, question, question_hash FROM answer_log WHERE seq = ?",
            (int(seq),),
        ).fetchone()
        if row is None:
            raise LookupError(f"answer log not found: {seq}")
        question = str(row["question"] or "").strip()
        if not question:
            raise ValueError("原始问题未记录，无法重生成（该回答产生于旧版本）")
        result = answer(self.kb, self.cfg, question, row["case_id"], corpus=self.corpus)
        new_seq = self.kb.execute("SELECT MAX(seq) FROM answer_log").fetchone()[0]
        with self.kb:
            cursor = self.kb.execute(
                "UPDATE answer_log SET superseded_by = ? "
                "WHERE seq = ? AND superseded_by IS NULL",
                (new_seq, row["seq"]),
            )
            if cursor.rowcount:
                self.kb.execute(
                    "UPDATE answer_log SET parent_seq = ? WHERE seq = ?",
                    (row["seq"], new_seq),
                )
        sections = result.get("sections") or []
        p0 = result.get("p0_warning") or {}
        parts = []
        if p0:
            parts.append(f"【安全警示】{p0.get('template')}")
        for section in sections:
            parts.append(f"{section.get('name')}: {section.get('text')}")
        if result.get("message"):
            parts.append(str(result["message"]))
        return {
            "ok": True,
            "seq": row["seq"],
            "new_seq": new_seq,
            "question": question,
            "refused": bool(result.get("refused")),
            "citations": len(result.get("citations") or []),
            "answer": "\n".join(parts),
        }

    def regenerate_many(self, seqs: list[int]) -> dict[str, Any]:
        results = []
        for seq in seqs[:20]:
            try:
                results.append(self.regenerate(int(seq)))
            except (LookupError, ValueError) as exc:
                results.append({"ok": False, "seq": seq, "error": str(exc)})
        succeeded = sum(1 for item in results if item.get("ok"))
        return {
            "ok": succeeded == len(results),
            "succeeded": succeeded,
            "failed": len(results) - succeeded,
            "results": results,
        }

    def compare(self, seq: int, regenerate: bool = False) -> dict[str, Any]:
        old_row = self.kb.execute(
            "SELECT seq, question, created_at, superseded_by, record_json FROM answer_log "
            "WHERE seq = ?",
            (int(seq),),
        ).fetchone()
        if old_row is None:
            raise LookupError(f"answer log not found: {seq}")
        new_row = None
        if old_row["superseded_by"]:
            new_row = self.kb.execute(
                "SELECT seq, question, created_at, superseded_by, record_json "
                "FROM answer_log WHERE seq = ?",
                (old_row["superseded_by"],),
            ).fetchone()
        if new_row is None:
            new_row = self.kb.execute(
                "SELECT seq, question, created_at, superseded_by, record_json "
                "FROM answer_log WHERE parent_seq = ? ORDER BY seq DESC LIMIT 1",
                (int(seq),),
            ).fetchone()
        if new_row is None and regenerate:
            regenerated = self.regenerate(seq)
            new_row = self.kb.execute(
                "SELECT seq, question, created_at, superseded_by, record_json "
                "FROM answer_log WHERE seq = ?",
                (regenerated["new_seq"],),
            ).fetchone()

        def summarize(row) -> dict[str, Any]:
            record = json.loads(row["record_json"] or "{}")
            sections = record.get("sections") or []
            text = "\n".join(
                f"{section.get('name')}: {section.get('text')}" for section in sections
            ) or str(record.get("message") or "")
            return {
                "seq": row["seq"],
                "question": row["question"] or "",
                "created_at": row["created_at"],
                "library_version": record.get("library_version"),
                "refused": bool(record.get("refused")),
                "p0_warning": record.get("p0_warning"),
                "sections": sections,
                "citations": record.get("citations") or [],
                "claims": record.get("claims") or [],
                "text": text,
            }

        old = summarize(old_row)
        diff: dict[str, Any] = {"text": [], "claims_changed": [], "citations_added": [], "citations_removed": []}
        if new_row is not None:
            new = summarize(new_row)
            diff["text"] = list(
                difflib.unified_diff(
                    old["text"].splitlines(), new["text"].splitlines(), lineterm=""
                )
            )
            old_claims = {item.get("claim_id"): item.get("value") for item in old["claims"]}
            new_claims = {item.get("claim_id"): item.get("value") for item in new["claims"]}
            for claim_id in sorted(set(old_claims) | set(new_claims)):
                if old_claims.get(claim_id) != new_claims.get(claim_id):
                    diff["claims_changed"].append(
                        {
                            "claim_id": claim_id,
                            "old_value": old_claims.get(claim_id),
                            "new_value": new_claims.get(claim_id),
                        }
                    )
            old_citations = {
                (item.get("doc_id"), item.get("page")) for item in old["citations"]
            }
            new_citations = {
                (item.get("doc_id"), item.get("page")) for item in new["citations"]
            }
            diff["citations_added"] = [list(item) for item in new_citations - old_citations]
            diff["citations_removed"] = [list(item) for item in old_citations - new_citations]
        else:
            new = None
        return {"ok": True, "old": old, "new": new, "diff": diff}

    def writeback(self, claim_id: str, version_id: str | None = None) -> dict[str, Any]:
        if not claim_id:
            raise ValueError("claim_id is required")
        return writeback_answers(self.kb, self.cfg, claim_id, version_id)

    def purge_questions(self, days: int | None = None) -> dict[str, Any]:
        purged = purge_answer_questions(self.kb, days)
        if purged:
            with self.kb:
                self.kb.execute(
                    "INSERT INTO kb_audit(event_seq, action, actor, detail_json, created_at) "
                    "VALUES (NULL, 'phi_purge', 'system', ?, datetime('now'))",
                    (json.dumps({"purged": purged, "days": days}, ensure_ascii=False),),
                )
        return {"ok": True, "purged": purged, "days": days}

    def sla_tick(self, cooldown_hours: float = 4.0) -> dict[str, Any]:
        summary = self.sla()
        if summary["overdue"] == 0:
            return {"notified": False, "reason": "no_overdue", "overdue": 0}
        last = self.kb.execute(
            "SELECT value FROM meta WHERE key = 'sla_last_notify'"
        ).fetchone()
        if last is not None:
            try:
                last_at = datetime.fromisoformat(str(last["value"]).replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - last_at).total_seconds() / 3600.0
                if elapsed < cooldown_hours:
                    return {
                        "notified": False,
                        "reason": "cooldown",
                        "overdue": summary["overdue"],
                    }
            except ValueError:
                pass
        try:
            result = self.notify_sla()
        except Exception as exc:  # noqa: BLE001 - scheduler keeps running
            return {"notified": False, "reason": str(exc), "overdue": summary["overdue"]}
        with self.kb:
            self.kb.execute(
                "INSERT INTO meta(key, value) VALUES('sla_last_notify', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),),
            )
        return {
            "notified": bool(result.get("ok")),
            "channels": result.get("channels", []),
            "overdue": summary["overdue"],
            "due_soon": summary["due_soon"],
        }

    def _writeback_claim(self, claim_id: str, version_id: str | None = None) -> dict[str, Any]:
        if os.environ.get("MPPKB_ANSWER_WRITEBACK", "0") != "1":
            return {"ok": True, "enabled": False, "written": 0}
        try:
            return writeback_answers(self.kb, self.cfg, claim_id, version_id)
        except Exception as exc:  # noqa: BLE001 - writeback must not fail the decision
            return {"ok": False, "error": str(exc), "written": 0}

    def decide(self, body: dict[str, Any]) -> dict[str, Any]:
        action = str(body.get("action") or "").strip()
        claim_id = str(body.get("claim_id") or "").strip()
        if action not in ("approve", "reject", "hold", "undo"):
            raise ValueError("action must be approve/reject/hold/undo")
        if not claim_id:
            raise ValueError("claim_id is required")
        result = decide(
            self.kb,
            self.cfg,
            action,
            claim_id,
            body.get("version_id"),
            body.get("reason"),
            body.get("note"),
        )
        self._catalog = None
        result["writeback"] = self._writeback_claim(claim_id, body.get("version_id"))
        return {"ok": True, "result": result}

    def case_run(self, case: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(case, dict) or not str(case.get("question") or "").strip():
            raise ValueError("case.question is required")
        return run_case(self.kb, self.cfg, self.corpus, case)

    def batch_impact(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(items, list) or not items:
            raise ValueError("items must be a non-empty array")
        if len(items) > 500:
            raise ValueError("at most 500 items per impact preview")
        return batch_impact(self.kb, items)

    def batch_decide(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(items, list) or not items:
            raise ValueError("items must be a non-empty array")
        if len(items) > 200:
            raise ValueError("at most 200 items per batch")
        result = batch_decide(self.kb, self.cfg, items)
        if result.get("succeeded"):
            self._catalog = None
            writebacks = []
            for item, item_result in zip(items, result.get("results") or []):
                if not item_result.get("ok"):
                    continue
                writebacks.append(
                    self._writeback_claim(
                        str(item.get("claim_id") or ""), item.get("version_id")
                    )
                )
            result["writebacks"] = writebacks
        return result


class _Handler(BaseHTTPRequestHandler):
    server_version = "mppkb-ui-bridge/0.2"
    bridge: KernelBridge
    web_dir: Path

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_static(self, relative: str) -> None:
        path = (self.web_dir / relative).resolve()
        if not path.is_file() or self.web_dir.resolve() not in path.parents:
            self._send_json(404, {"error": {"code": "not_found", "message": "not found"}})
            return
        if relative == "index.html":
            text = path.read_text(encoding="utf-8")
            if "</body>" in text:
                text = text.replace("</body>", INJECT_HTML + "\n</body>")
            else:
                text += INJECT_HTML
            self._send_bytes(text.encode("utf-8"), "text/html; charset=utf-8")
            return
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        self._send_bytes(data, content_type)

    def _queue_args(self, parsed) -> dict[str, Any]:
        params = parse_qs(parsed.query)

        def one(name: str, default: str = "") -> str:
            return (params.get(name) or [default])[0]

        try:
            page = int(one("page", "1"))
            page_size = int(one("page_size", "20"))
        except ValueError:
            page, page_size = 1, 20
        return {
            "page": page,
            "page_size": page_size,
            "group_by": one("group_by", "intervention"),
            "intervention": one("intervention"),
            "severity": one("severity"),
            "q": one("q"),
            "assignee": one("assignee"),
            "sla": one("sla"),
        }

    def _send_csv(self, text: str, filename: str) -> None:
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in STATIC_FILES:
            self._send_static(STATIC_FILES[path])
            return
        if path == "/api/health":
            catalog = self.bridge.catalog()
            self._send_json(
                200,
                {
                    "status": "ok",
                    "graph_valid": True,
                    "nodes": len(catalog["nodes"]),
                    "edges": len(catalog["edges"]),
                    "kernel": "mppkb",
                },
            )
            return
        if path == "/api/evidence":
            self._send_json(200, self.bridge.catalog())
            return
        if path == "/api/todos":
            self._send_json(200, collect_todos(self.bridge.kb))
            return
        if path == "/api/queue":
            self._send_json(200, self.bridge.queue_page(**self._queue_args(parsed)))
            return
        if path == "/api/queue/keys":
            args = self._queue_args(parsed)
            self._send_json(
                200,
                self.bridge.queue_keys(
                    intervention=args["intervention"],
                    severity=args["severity"],
                    q=args["q"],
                    assignee=args["assignee"],
                    sla=args["sla"],
                ),
            )
            return
        if path == "/api/export/queue.csv":
            args = self._queue_args(parsed)
            csv_text = self.bridge.queue_csv(
                intervention=args["intervention"],
                severity=args["severity"],
                q=args["q"],
                assignee=args["assignee"],
                sla=args["sla"],
            )
            self._send_csv(csv_text, "mppkb-review-queue.csv")
            return
        if path == "/api/reviewers":
            self._send_json(200, self.bridge.reviewers())
            return
        if path == "/api/sla":
            self._send_json(200, self.bridge.sla())
            return
        if path == "/api/reviews":
            params = parse_qs(parsed.query)

            def review_arg(name: str, default: str = "") -> str:
                return (params.get(name) or [default])[0]

            try:
                limit = int(review_arg("limit", "50"))
            except ValueError:
                limit = 50
            self._send_json(
                200,
                self.bridge.ledger(
                    limit=limit,
                    reviewer=review_arg("reviewer"),
                    action=review_arg("action"),
                ),
            )
            return
        if path == "/api/export/reviews.csv":
            params = parse_qs(parsed.query)
            reviewer = (params.get("reviewer") or [""])[0]
            action = (params.get("action") or [""])[0]
            self._send_csv(
                self.bridge.reviews_export(reviewer=reviewer, action=action),
                "mppkb-review-ledger.csv",
            )
            return
        if path == "/api/reason-codes":
            self._send_json(200, self.bridge.reason_codes())
            return
        self._send_json(404, {"error": {"code": "not_found", "message": "not found"}})

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        body = self._read_json()
        try:
            if path == "/api/analyze":
                question = str(body.get("question") or "").strip()
                if not question:
                    self._send_json(
                        400,
                        {"error": {"code": "empty_question", "message": "请输入临床问题。"}},
                    )
                    return
                self._send_json(200, self.bridge.analyze(question))
                return
            if path == "/api/model/test":
                self._send_json(
                    200,
                    {
                        "connected": True,
                        "model_name": "mppkb-kernel（确定性回答，不调用外部模型）",
                    },
                )
                return
            if path == "/api/decide":
                self._send_json(200, self.bridge.decide(body))
                return
            if path == "/api/batch/impact":
                self._send_json(200, self.bridge.batch_impact(body.get("items") or []))
                return
            if path == "/api/batch/decide":
                self._send_json(200, self.bridge.batch_decide(body.get("items") or []))
                return
            if path == "/api/assign":
                items = body.get("items") or []
                reviewer = str(body.get("reviewer") or "").strip()
                if not reviewer:
                    raise ValueError("reviewer is required")
                self._send_json(200, self.bridge.assign(items, reviewer))
                return
            if path == "/api/answers/regenerate":
                seqs = body.get("seqs")
                if seqs is None and body.get("seq") is not None:
                    seqs = [body.get("seq")]
                self._send_json(200, self.bridge.regenerate_many(seqs or []))
                return
            if path == "/api/answers/compare":
                self._send_json(
                    200,
                    self.bridge.compare(
                        int(body.get("seq") or 0), bool(body.get("regenerate"))
                    ),
                )
                return
            if path == "/api/answers/writeback":
                self._send_json(
                    200,
                    self.bridge.writeback(
                        str(body.get("claim_id") or ""), body.get("version_id")
                    ),
                )
                return
            if path == "/api/maintenance/purge-questions":
                days = body.get("days")
                self._send_json(
                    200, self.bridge.purge_questions(int(days) if days else None)
                )
                return
            if path == "/api/sla/notify":
                self._send_json(200, self.bridge.notify_sla())
                return
            if path == "/api/case/run":
                case = body.get("case") or body
                self._send_json(200, self.bridge.case_run(case))
                return
        except Exception as exc:  # noqa: BLE001 - JSON error boundary for the UI
            self._send_json(400, {"error": {"code": "kernel_error", "message": str(exc)}})
            return
        self._send_json(404, {"error": {"code": "not_found", "message": "not found"}})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve MPP_Project UI backed by mppkb kernel")
    parser.add_argument("--repo", required=True, help="path to the MPP_Project checkout")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args(argv)
    web_dir = Path(args.repo) / "web"
    if not (web_dir / "index.html").is_file():
        raise SystemExit(f"web/index.html not found under {args.repo}")
    cfg = load_config()
    bridge = KernelBridge(cfg)
    handler = type(
        "BridgeHandler",
        (_Handler,),
        {"bridge": bridge, "web_dir": web_dir},
    )
    server = HTTPServer((args.host, args.port), handler)
    if os.environ.get("MPPKB_SLA_SCHEDULER", "0") == "1":
        interval = max(30, int(os.environ.get("MPPKB_SLA_INTERVAL_SECONDS", "300")))
        cooldown = float(os.environ.get("MPPKB_SLA_NOTIFY_COOLDOWN_HOURS", "4"))
        retention = int(os.environ.get("MPPKB_QUESTION_RETENTION_DAYS", "30"))

        def loop() -> None:
            while True:
                try:
                    worker = KernelBridge(cfg)
                    worker.sla_tick(cooldown)
                    worker.purge_questions(retention)
                except Exception:  # noqa: BLE001 - scheduler keeps running
                    pass
                time.sleep(interval)

        threading.Thread(target=loop, daemon=True).start()
    print(f"serving MPP_Project UI on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
