/* Filtering & Scoring. Server-rendered state arrives from the inline preamble
   in deletion_score_explorer.html; this file is deferred and cannot carry
   template expressions. */

// The controls that are conditional on something OTHER than the run: a
// dependent field follows its own toggle, and the favorites toggle follows the
// Jellyfin connection. The unlock pass must never hand these back just because
// a run ended, so they are re-derived here rather than blanket-enabled.
function _applyExplorerFieldDeps(){
  const el=id=>document.getElementById(id);
  if(el('c-cutoff'))el('c-cutoff').disabled=!el('c-cutoff-on')?.checked;
  if(el('c-tie'))el('c-tie').disabled=!el('c-tie-on')?.checked;
  if(el('c-jf-fav'))el('c-jf-fav').disabled=!expJfConnected;
}
// Same helper, same ordering as the Configuration page's sections: release,
// let the real rules decide, then lay the run lock over the top. Sweeping each
// section body means a control added to one later is covered without being
// listed here — and only the bodies, so the headers still open. A run stops
// you CHANGING a setting; it should not stop you reading what one says.
function _applyExplorerRunLock(){
  const locked=expRunActive;
  const card=document.getElementById('filter-score-card');
  const note=document.getElementById('exp-run-lock-note');
  const bodies=card?[...card.querySelectorAll('.accordion-body')]:[];
  if(note)note.hidden=!locked;
  if(!locked)bodies.forEach(b=>window.prSetRunLock(b,false));
  _applyExplorerFieldDeps();
  updateCfgButtons();
  if(locked)bodies.forEach(b=>window.prSetRunLock(b,true));
  card?.classList.toggle('section-run-ghost',locked);
}
window.prOnStatusPoll=function(d){
  const was=expRunActive;
  expRunActive=!!(d&&d.run_active);
  if(was!==expRunActive){
    _applyExplorerRunLock();
    // A run just finished: it rewrote the library snapshot — pull the fresh one.
    if(was&&!expRunActive&&typeof loadPool==='function')loadPool();
  }
};
const clamp=(x,a,b)=>Math.min(b,Math.max(a,x));
function voteFromSlider(){
  const lv=Number(document.getElementById('s-vot').value);
  return lv<=0?0:Math.max(1,Math.round(Math.pow(10,lv)));
}
function sliderValueFromVotes(v){
  v=Number(v||0);
  return v<=0?0:clamp(Math.log10(v),VOTE_LOG_MIN,VOTE_LOG_MAX);
}
const htmlEsc=prHtmlEsc;  // shared with Configuration (base.html)
function num(id,fallback){
  // Blank must take the fallback: Number('') is 0, which silently turned a
  // cleared cutoff box into CUTOFF=0 (every rated movie "filtered") and saved
  // MAX_IMDB_RATING: 0 to the server. Garbage takes the fallback too.
  return prBlankNumber(id,fallback)??fallback;
}
// The full Filtering & Scoring config, one field per rule: MOVIES_ON / TV_ON
// (the per-type cleanup scope; absent = on, the shipped behavior), BAL (score
// balance), CUTOFF (max IMDb rating or null), UNPLAYED (skip unplayed),
// GRACE (grace period days), JF_FAV (protect Jellyfin favorites), TIE
// (near-tie window width in points, or null = file size optimization off).
// All of them preview live; Save persists them all.
function canonCfg(c){
  c=c||{};
  let cutoff=null;
  if(c.CUTOFF!=null&&c.CUTOFF!==''){
    const n=Number(c.CUTOFF);
    if(Number.isFinite(n))cutoff=clamp(Math.round(n*10)/10,0,10);
  }
  let tie=null;
  if(c.TIE!=null&&c.TIE!==''){
    const n=Number(c.TIE);
    if(Number.isFinite(n))tie=clamp(Math.round(n*10)/10,0.5,25);
  }
  return{
    // Absent reads as OFF, matching the server. Defaulting these ON would show
    // both switches ticked over a config that has them off, and saving that
    // form would turn cleanup on for a library nobody opted in.
    MOVIES_ON:!!c.MOVIES_ON,
    TV_ON:!!c.TV_ON,
    BAL:clamp(Math.round(Number(c.BAL ?? 50)||0),0,100),
    CUTOFF:cutoff,
    UNPLAYED:!!c.UNPLAYED,
    GRACE:Math.max(0,Math.round(Number(c.GRACE)||0)),
    JF_FAV:!!c.JF_FAV,
    TIE:tie,
    STALE:clamp(Math.round(Number(c.STALE ?? 36)||36),1,120),
    TVBUMP:clamp(Math.round((Number(c.TVBUMP ?? 10)||0)*10)/10,0,25),
    TVWEIGHT:clamp(Math.round(Number(c.TVWEIGHT ?? 100)||100),100,200),
    TVELIG:['oldest','except_newest','all'].includes(c.TVELIG)?c.TVELIG:'oldest',
    TVEPCAP:clamp(Math.round(Number(c.TVEPCAP ?? 50)||0),0,999),
  };
}
function readCfg(){
  const cutoffOn=!!document.getElementById('c-cutoff-on')?.checked;
  const tieOn=!!document.getElementById('c-tie-on')?.checked;
  const jfEl=document.getElementById('c-jf-fav'); // disabled (not absent) when Jellyfin is off
  return canonCfg({
    MOVIES_ON:!!document.getElementById('c-movies-on')?.checked,
    TV_ON:!!document.getElementById('c-tv-on')?.checked,
    BAL:num('c-bal',50),
    CUTOFF:cutoffOn?num('c-cutoff',7.5):null,
    UNPLAYED:!!document.getElementById('c-unplayed')?.checked,
    GRACE:num('c-grace',0),
    JF_FAV:jfEl?jfEl.checked:!!(savedCfg&&savedCfg.JF_FAV),
    TIE:tieOn?num('c-tie',2):null,
    STALE:num('c-stale',36),
    TVBUMP:num('c-tv-bump',10),
    TVWEIGHT:num('c-tv-weight',100),
    TVELIG:document.getElementById('c-tv-elig')?.value,
    TVEPCAP:num('c-tv-eps-cap',50),
  });
}
function cfgFromServer(cfg){
  return canonCfg({
    MOVIES_ON:cfg.MOVIE_CLEANUP_ENABLED,
    TV_ON:cfg.TV_CLEANUP_ENABLED,
    BAL:cfg.SCORE_BALANCE ?? 50,
    CUTOFF:cfg.MAX_IMDB_RATING ?? null,
    UNPLAYED:cfg.SKIP_UNPLAYED_MOVIES,
    GRACE:cfg.GRACE_PERIOD_DAYS,
    JF_FAV:cfg.PROTECT_JELLYFIN_FAVORITES,
    TIE:cfg.NEAR_TIE_PTS===undefined?2:cfg.NEAR_TIE_PTS,
    STALE:cfg.MAX_STALENESS_MONTHS===undefined?36:cfg.MAX_STALENESS_MONTHS,
    TVBUMP:cfg.TV_SERIES_WATCH_BUMP===undefined?10:cfg.TV_SERIES_WATCH_BUMP,
    TVWEIGHT:cfg.TV_WATCH_WEIGHT===undefined?100:cfg.TV_WATCH_WEIGHT,
    TVELIG:cfg.TV_SEASON_ELIGIBILITY,
    TVEPCAP:cfg.TV_MAX_SEASON_EPISODES===undefined?50:cfg.TV_MAX_SEASON_EPISODES,
  });
}
function cfgPayload(c){
  c=canonCfg(c);
  return{
    MOVIE_CLEANUP_ENABLED:c.MOVIES_ON,
    TV_CLEANUP_ENABLED:c.TV_ON,
    SCORE_BALANCE:c.BAL,
    MAX_IMDB_RATING:c.CUTOFF,
    SKIP_UNPLAYED_MOVIES:c.UNPLAYED,
    GRACE_PERIOD_DAYS:c.GRACE,
    PROTECT_JELLYFIN_FAVORITES:c.JF_FAV,
    NEAR_TIE_PTS:c.TIE,
    MAX_STALENESS_MONTHS:c.STALE,
    TV_SERIES_WATCH_BUMP:c.TVBUMP,
    TV_WATCH_WEIGHT:c.TVWEIGHT,
    TV_SEASON_ELIGIBILITY:c.TVELIG,
    TV_MAX_SEASON_EPISODES:c.TVEPCAP,
  };
}
function cfgEq(a,b){
  a=canonCfg(a);b=canonCfg(b);
  return a.MOVIES_ON===b.MOVIES_ON&&a.TV_ON===b.TV_ON
    &&a.BAL===b.BAL&&a.CUTOFF===b.CUTOFF&&a.UNPLAYED===b.UNPLAYED
    &&a.GRACE===b.GRACE&&a.JF_FAV===b.JF_FAV&&a.TIE===b.TIE&&a.STALE===b.STALE
    &&a.TVBUMP===b.TVBUMP&&a.TVWEIGHT===b.TVWEIGHT&&a.TVELIG===b.TVELIG
    &&a.TVEPCAP===b.TVEPCAP;
}
function setFormFromCfg(c){
  c=canonCfg(c);
  const set=(id,fn)=>{const el=document.getElementById(id);if(el)fn(el);};
  set('c-movies-on',el=>el.checked=c.MOVIES_ON);
  set('c-tv-on',el=>el.checked=c.TV_ON);
  set('c-bal',el=>el.value=c.BAL);
  set('c-cutoff-on',el=>el.checked=c.CUTOFF!=null);
  // Disabled optional fields keep their last entered value visible. The
  // run lock wins over per-field enablement (a load during an active run
  // must not re-enable the inputs the lock just froze).
  set('c-cutoff',el=>{if(c.CUTOFF!=null)el.value=c.CUTOFF;else if(expLastCutoff!=null)el.value=expLastCutoff;el.disabled=c.CUTOFF==null||expRunActive;});
  set('c-unplayed',el=>el.checked=c.UNPLAYED);
  set('c-grace',el=>el.value=c.GRACE);
  set('c-jf-fav',el=>el.checked=c.JF_FAV);
  set('c-tie-on',el=>el.checked=c.TIE!=null);
  set('c-tie',el=>{if(c.TIE!=null)el.value=c.TIE;else if(expLastTie!=null)el.value=expLastTie;el.disabled=c.TIE==null||expRunActive;});
  set('c-stale',el=>el.value=c.STALE);
  set('c-tv-bump',el=>el.value=c.TVBUMP);
  set('c-tv-weight',el=>el.value=c.TVWEIGHT);
  set('c-tv-elig',el=>el.value=c.TVELIG);
  set('c-tv-eps-cap',el=>el.value=c.TVEPCAP);
  _syncTvKnobBadges();
  _expValidateAll();
}
// The TV sliders carry live value badges (a range input shows no number).
function _syncTvKnobBadges(){
  const w=num('c-tv-weight',100), b=num('c-tv-bump',10);
  const we=document.getElementById('v-tv-weight');
  if(we)we.textContent=(w/100).toFixed(2).replace(/\.?0+$/,'')+(w===100?' watch':' watches');
  const be=document.getElementById('v-tv-bump');
  if(be)be.textContent=b+' pts';
}
function onExpTvKnobInput(){_syncTvKnobBadges();onExpFilterInput();}
// ── Field validation: red flag on blur with a blank or out-of-range value ──
// An enabled field needs a real value — blank is invalid, not a silent
// default, so a cleared cutoff can never save as 7.5. A disabled field is
// never flagged (unchecking is the off switch).
const _expFieldRules={
  'c-cutoff':{on:()=>!!document.getElementById('c-cutoff-on')?.checked&&!document.getElementById('c-cutoff')?.disabled,ok:v=>v>0&&v<=10},
  'c-tie':{on:()=>!!document.getElementById('c-tie-on')?.checked&&!document.getElementById('c-tie')?.disabled,ok:v=>v>=0.5&&v<=25},
  'c-stale':{on:()=>!document.getElementById('c-stale')?.disabled,ok:v=>v>=1&&v<=120},
  'c-tv-eps-cap':{on:()=>!document.getElementById('c-tv-eps-cap')?.disabled,ok:v=>v>=0&&v<=999&&Number.isInteger(v)},
};
function _expFieldInvalid(id){
  const rule=_expFieldRules[id];
  if(!rule||!rule.on())return false;
  const v=prNumOrNull(prFieldRaw(id));
  return v===null||!rule.ok(v);
}
// A red field inside a CLOSED section is invisible, and the only symptom left
// is a Save button that will not go. The header marker says which one to open.
const _expSectionIssues=prSectionIssues('filter-score-card',{selector:'.field-invalid'});
function _expValidateField(id){
  const invalid=_expFieldInvalid(id);
  const el=document.getElementById(id);
  const card=el?.closest('.filter-score-card');
  // Always armed; focus only suppresses NEW flags (see prInvalidVisible).
  const visible=prInvalidVisible(invalid,true,el,card);
  prApplyFieldFlag(card,document.getElementById(id+'-error'),el,visible);
  _expSectionIssues.schedule();
  return !invalid;
}
function _expValidateAll(){
  return ['c-cutoff','c-tie','c-stale','c-tv-eps-cap'].map(_expValidateField).every(Boolean);
}
function _expAnyFieldInvalid(){
  return ['c-cutoff','c-tie','c-stale','c-tv-eps-cap'].some(_expFieldInvalid);
}
let _filterTableTimer=null;
function onExpFilterInput(){
  // Live preview: the paged library table is debounced. The single-movie panel
  // recomputes immediately — Max staleness feeds the score, so a stale panel
  // would show a different Retention than the freshly re-scored rows.
  appliedCfg=readCfg();
  _expValidateAll();
  updateCfgButtons();
  go();
  clearTimeout(_filterTableTimer);
  _filterTableTimer=setTimeout(renderT,90);
}
function onExpCutoffToggle(){
  const on=!!document.getElementById('c-cutoff-on')?.checked;
  const inp=document.getElementById('c-cutoff');
  if(inp)inp.disabled=!on;
  onExpFilterInput();
}
function onExpTieToggle(){
  const on=!!document.getElementById('c-tie-on')?.checked;
  const inp=document.getElementById('c-tie');
  if(inp)inp.disabled=!on;
  onExpFilterInput();
}
function onExpBalanceInput(){
  // Live preview, nothing saved until Save. Single-movie breakdown is cheap
  // (tracks the drag); the paged library table is debounced to the drag's end.
  appliedCfg=readCfg();
  updateCfgButtons();
  go();
  clearTimeout(_liveTableTimer);
  _liveTableTimer=setTimeout(renderT,90);
}
let _liveTableTimer=null;
function updateCfgButtons(){
  const cur=readCfg();
  const dirty=savedCfg?!cfgEq(cur,savedCfg):false;
  const invalid=_expAnyFieldInvalid();
  const saveBtn=document.getElementById('btn-cfg-save');
  const revertBtn=document.getElementById('btn-cfg-revert');
  if(saveBtn){
    saveBtn.disabled=cfgSaving||!dirty||expRunActive||invalid;
    // "Saving" with an ANIMATED ellipsis (.pending-ellipsis ::after) — the same
    // affordance the Config page's Save uses — so it's unmistakable that the save
    // is still in flight (and why leaving the page is being guarded). The label
    // text carries no dots; the animation supplies them.
    saveBtn.textContent=cfgSaving?'Saving':'Save';
    saveBtn.classList.toggle('btn-busy',cfgSaving);
    saveBtn.classList.toggle('pending-ellipsis',cfgSaving);
    saveBtn.title=expRunActive?'A run is active. Try again when it finishes.':(invalid?'Fix the highlighted fields first.':'');
  }
  if(revertBtn){
    // An invalid value may canonicalize back to the saved one (not "dirty"),
    // but Revert must still be able to restore the field.
    revertBtn.disabled=cfgSaving||(!dirty&&!invalid)||expRunActive;
    revertBtn.classList.toggle('btn-revert-active',(dirty||invalid)&&!cfgSaving&&!expRunActive);
    revertBtn.title=expRunActive?'A run is active. Try again when it finishes.':'';
  }
}
function revertCfg(){
  if(cfgSaving||!savedCfg)return;
  setFormFromCfg(savedCfg);
  appliedCfg=canonCfg(savedCfg);
  all();
  updateCfgButtons();
}
async function saveCfgToConfig(){
  // Returns true when the saved config now matches the form (including the
  // no-op case where nothing was dirty), false when the save failed.
  const cur=readCfg();
  if(cfgSaving)return false;
  if(expRunActive){
    if(typeof showToast==='function')showToast('A run is active. Try again when it finishes.','warning');
    return false;
  }
  if(!_expValidateAll()){
    if(typeof showToast==='function')showToast('Fix the highlighted fields first.','warning');
    return false;
  }
  if(savedCfg&&cfgEq(cur,savedCfg))return true;
  appliedCfg=cur;
  all();
  cfgSaving=true;
  updateCfgButtons();
  let ok=false;
  try{
    // Text still sitting in a disabled optional field rides along so the
    // server remembers it (shown grayed out; survives restarts).
    const payload=cfgPayload(cur);
    if(cur.CUTOFF==null){
      const v=num('c-cutoff',null);
      if(v!=null&&v>0&&v<=10)payload._MAX_IMDB_RATING_LAST=v;
    }
    if(cur.TIE==null){
      const v=num('c-tie',null);
      if(v!=null&&v>=0.5&&v<=25)payload._NEAR_TIE_PTS_LAST=v;
    }
    const r=await fetch('/api/score-config',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload)
    });
    const data=await r.json().catch(()=>({}));
    if(!r.ok||!data.ok)throw new Error(data.error||'Could not save scoring config.');
    if(data.config){
      // The response is authoritative: null means an enabled save cleared
      // the memory, so mirror it rather than keeping a stale value.
      expLastCutoff=data.config._MAX_IMDB_RATING_LAST??null;
      expLastTie=data.config._NEAR_TIE_PTS_LAST??null;
    }
    savedCfg=cfgFromServer(data.config||cfgPayload(cur));
    setFormFromCfg(savedCfg);
    appliedCfg=canonCfg(savedCfg);
    all();
    ok=true;
    if(typeof showToast==='function')showToast('Filtering & Scoring saved','success');
  }catch(err){
    if(typeof showToast==='function')showToast(err.message||String(err),'danger');
  }finally{
    cfgSaving=false;
    updateCfgButtons();
  }
  return ok;
}

// Leaving mid-save aborts the POST (the save may never commit), and navigating
// away from a dirty form silently discards the edits — guard both, mirroring the
// Configuration page. The animated "Saving" label makes the in-flight case
// visible; this stops an accidental click away from throwing it out.
window.addEventListener('beforeunload',(event)=>{
  let dirty=false;
  try{ dirty=!!savedCfg && !cfgEq(readCfg(),savedCfg); }catch{ dirty=false; }
  if(!cfgSaving && !dirty) return;
  event.preventDefault();
  event.returnValue='';
});

function readPruneGb(){
  const v=Number(document.getElementById('over-gb').value);
  return Number.isFinite(v)?Math.max(0,v):0;
}
let _pruneTimer=null;
function onPruneTargetInput(){
  // Live: the prune plan follows the typed target (debounced so each
  // keystroke doesn't rebuild the table). Simulation only — never saved.
  clearTimeout(_pruneTimer);
  _pruneTimer=setTimeout(()=>{appliedPruneGb=readPruneGb();renderT();},180);
}
function gcfg(){
  if(!appliedCfg)appliedCfg=readCfg();
  return appliedCfg;
}
// ── Retention scoring — EXACT mirror of the engine's compute_retention_score ──
function voteConfidence(votes){
  if(votes===null||votes===undefined)return SCORING.VOTE_CONF_UNKNOWN;
  const v=Math.floor(Number(votes)||0);
  const floor=SCORING.VOTE_CONF_FLOOR;
  if(v<=0)return floor;
  return Math.min(1,floor+(1-floor)*(Math.log10(v)/SCORING.VOTE_CONF_FULL_LOG10));
}
function balanceWeights(bal){
  // Mirror of the engine's score_balance_weights(): linear, weights sum to 1.
  // Center (50) = even 50/50; ends = pure history / pure IMDb.
  const t=clamp(Number(bal??50),0,100)/100;
  const q=t;  // linear: quality = balance/100
  return{h:1-q,q};
}
function imdbInUse(cfg){
  // Mirror of engine.imdb_dataset_needed(): IMDb has a say only when the dial
  // gives it weight or a rating cutoff is set. At 100% watch history with no
  // cutoff, no-IMDb-data is not a skip reason and the rating tiebreak is off.
  cfg=cfg||gcfg();
  return balanceWeights(cfg.BAL).q>0 || cfg.CUTOFF!=null;
}
function retentionBreakdown(m,cfg){
  // Both sides are normalized 0–100, then blended by the balance weights.
  // Breakdown values are already weighted, so they sum to the 0–100 score.
  const w=balanceWeights(cfg.BAL);
  const b={};
  const plays=Math.max(0,Math.floor(m.playCount||0));
  b.usage=SCORING.USAGE_MAX_PTS*Math.min(1,Math.log1p(plays)/Math.log1p(SCORING.USAGE_FULL_PLAYS))*w.h;
  // Mirror of the engine: recency uses last-played, falling back to added when
  // never watched — so a recently-added-but-unwatched movie still earns recency.
  const users=Math.max(0,Math.floor(m.users||0));
  const lpd=(m.lastPlayedDays!=null)?m.lastPlayedDays:null;
  const recDays=(lpd!=null)?lpd:((m.addedDays!=null)?m.addedDays:null);
  const staleScale=(Number(gcfg().STALE)||SCORING.RECENCY_DEFAULT_MONTHS)/SCORING.RECENCY_DEFAULT_MONTHS;
  // Distinct watchers slow the decay (mirror of the engine): each unique user
  // stretches the effective staleness window (recency tiers + shelf tail).
  const decayMult=Math.min(SCORING.USER_DECAY_MAX_MULT,1+SCORING.USER_DECAY_PER_USER*users);
  const effScale=staleScale*decayMult;
  let rec=0;
  if(recDays!=null){
    for(const [maxDays,pts] of SCORING.RECENCY_TIERS){ if(recDays<=maxDays*effScale){rec=pts;break;} }
  }
  b.recency=rec*w.h;
  b.multi_user=Math.min(SCORING.MULTI_USER_PTS*users,SCORING.MULTI_USER_MAX_PTS)*w.h;
  b.imdb=(Number.isFinite(m.rating)&&m.rating>0)
    ?Math.min(m.rating*10*voteConfidence(m.votes>0?m.votes:0),100)*w.q:0;
  // Soft shelf (mirror of the engine): continues the recency curve past its last
  // tier (the staleness cliff), reading the SAME recDays as the tiers above.
  // Tent weight (w.h * ramp): 0 at both dial ends, peaks mid-blend.
  let shelfPts=0;
  if(recDays!=null){
    const cliffDays=SCORING.RECENCY_TIERS[SCORING.RECENCY_TIERS.length-1][0]*effScale;
    const spanDays=cliffDays*SCORING.SHELF_SPAN_MULT;
    if(recDays>cliffDays&&spanDays>0){
      const frac=1-(recDays-cliffDays)/spanDays;
      shelfPts=SCORING.SHELF_MAX_PTS*Math.max(0,Math.min(1,frac));
    }
  }
  const shelfRamp=SCORING.SHELF_RAMP_FULL_Q>0?Math.min(1,w.q/SCORING.SHELF_RAMP_FULL_Q):1;
  b.shelf=shelfPts*w.h*shelfRamp;
  const retention=b.usage+b.recency+b.multi_user+b.imdb+b.shelf;
  return{breakdown:b,retention};
}
// Near-tie window width in points — the File size optimization setting
// (TIE; null when disabled). Mirrors the engine's NEAR_TIE_PTS.
function tieWindow(){
  const w=Number(gcfg().TIE);
  return Number.isFinite(w)&&w>0?w:null;
}
function scoreOne(m,cfg){
  // Score AND eligibility both follow the preview config, so the table shows
  // exactly what a run with these settings would skip. Checks mirror the
  // engine's skip order: movie cleanup switch → protected collection →
  // Jellyfin favorite → no IMDb data → IMDb rating cutoff → grace period →
  // unplayed.
  const sc={...m,...retentionBreakdown(m,cfg)};
  if(!cfg.MOVIES_ON)sc.status='mov_off';
  else if(m.protected)sc.status='protected';
  else if(cfg.JF_FAV&&m.favorite)sc.status='favorite';
  else if(imdbInUse(cfg)&&(m.rating==null||!(m.votes>0)))sc.status='nodata';
  else if(cfg.CUTOFF!=null&&m.rating>cfg.CUTOFF)sc.status='cutoff';
  else if(cfg.GRACE>0&&m.addedDays!=null&&m.addedDays<cfg.GRACE)sc.status='grace';
  else if(cfg.UNPLAYED&&!(m.playCount>0||m.lastPlayedDays!=null))sc.status='unplayed';
  else sc.status='ok';
  return sc;
}
const FILTER_LABELS={mov_off:'movie cleanup off',protected:'protected',favorite:'favorite',nodata:'no IMDb data',cutoff:'rating cutoff',grace:'grace period',unplayed:'unplayed',tv_off:'TV cleanup off',tv_oos:'off monitored paths',tv_latest:'latest season',tv_not_oldest:'not the oldest season',tv_newest:'newest season',tv_bigseason:'over the episode cap'};
// One TV SEASON's eligibility, mirroring _tv_season_plan's exclusion ladder:
// seasons are IN the pool, so an unfiltered season is eligible and ranks in
// the same deletion order as the movies. Whole-series shields first (scope,
// protected, favorite, grace, IMDb), then the per-season rules (the latest
// season of a not-known-ended show, skip-unplayed).
function seasonStatus(m,cfg){
  if(!cfg.TV_ON)return 'tv_off';
  if(!m.tvInScope)return 'tv_oos';
  if(m.protected)return 'protected';
  if(cfg.JF_FAV&&m.favorite)return 'favorite';
  // Rung order mirrors _tv_season_plan exactly, so a season shielded by two
  // rules is labeled with the SAME one the run's plan counts it under.
  if(imdbInUse(cfg)&&m.rating==null)return 'nodata';
  if(cfg.CUTOFF!=null&&m.rating!=null&&m.rating>cfg.CUTOFF)return 'cutoff';
  if(m.latestOfContinuing)return 'tv_latest';
  // Above the cap the row is a whole show filed under one season number,
  // not a season. Ranked above the eligibility rule for the same reason the
  // plan ranks it there: a flattened show IS its own oldest season, so
  // "waiting its turn" would be the wrong thing to read.
  if(cfg.TVEPCAP>0&&(m.seasonEps||0)>cfg.TVEPCAP)return 'tv_bigseason';
  if(cfg.TVELIG==='oldest'&&!m.isOldestSeason)return 'tv_not_oldest';
  if(cfg.TVELIG==='except_newest'&&m.isNewestSeason)return 'tv_newest';
  // Grace on the SEASON's own added date (its newest episode file), falling
  // back to the show's — a brand-new season of an old show is graced too.
  const sAdded=(m.seasonAddedDays!=null)?m.seasonAddedDays:m.addedDays;
  if(cfg.GRACE>0&&sAdded!=null&&sAdded<cfg.GRACE)return 'grace';
  if(cfg.UNPLAYED&&!(m.seasonEpsWatched>0)&&m.seasonLastPlayedDays==null)return 'unplayed';
  return 'ok';
}
// One TV SEASON's retention score (0-100, higher = keep) — the mirror of
// season_retention_score (scoring_constants.py), the same curve family as
// retentionBreakdown at season grain: usage runs the MOVIE play curve on the
// season's plays expressed as movie-watch equivalents (plays ÷ episodes ×
// the TVWEIGHT knob — playing a whole season once = one movie watch at
// 100%), recency reads the season's last watch (falling back to the series'
// added date), multi-user and the decay stretch read THIS SEASON's watchers
// exactly as a movie reads its own, the all-season watch boost lifts EVERY
// season of a watched show (knob: TVBUMP, points at a fully-watched series),
// and the IMDb side reads the series rating. The history side clamps at 100
// before weighting so the boost enriches the blend without pushing seasons
// onto a different scale than movies.
function seasonScore(m,cfg){
  cfg=cfg||gcfg();
  const w=balanceWeights(cfg.BAL);
  const eps=m.seasonEps||0, wa=m.seasonEpsWatched||0;
  const plays=(m.seasonPlays!=null)?m.seasonPlays:wa;
  const weight=Math.max(1,Math.min(2,(Number(cfg.TVWEIGHT)||100)/100));
  const effPlays=eps>0?(plays/eps)*weight:0;
  const usage=SCORING.USAGE_MAX_PTS*Math.min(1,Math.log1p(effPlays)/Math.log1p(SCORING.USAGE_FULL_PLAYS));
  const users=Math.max(0,Math.floor(((m.seasonUsers!=null)?m.seasonUsers:m.users)||0));
  const staleScale=(Number(cfg.STALE)||SCORING.RECENCY_DEFAULT_MONTHS)/SCORING.RECENCY_DEFAULT_MONTHS;
  const effScale=staleScale*Math.min(SCORING.USER_DECAY_MAX_MULT,1+SCORING.USER_DECAY_PER_USER*users);
  const recDays=(m.seasonLastPlayedDays!=null)?m.seasonLastPlayedDays
    :((m.seasonAddedDays!=null)?m.seasonAddedDays:((m.addedDays!=null)?m.addedDays:null));
  let rec=0,shelfPts=0;
  if(recDays!=null){
    for(const [maxDays,pts] of SCORING.RECENCY_TIERS){ if(recDays<=maxDays*effScale){rec=pts;break;} }
    const cliff=SCORING.RECENCY_TIERS[SCORING.RECENCY_TIERS.length-1][0]*effScale;
    const span=cliff*SCORING.SHELF_SPAN_MULT;
    if(recDays>cliff&&span>0)shelfPts=SCORING.SHELF_MAX_PTS*Math.max(0,Math.min(1,1-(recDays-cliff)/span));
  }
  const mu=Math.min(SCORING.MULTI_USER_PTS*users,SCORING.MULTI_USER_MAX_PTS);
  // Log curve, not a linear fraction: one watched episode of a big show is a
  // visible nudge to every season, growing toward 1.0 as the show is consumed.
  const sWatched=m.tvEpisodesWatched||0;
  const sf=(m.tvEpisodes>1)?Math.min(1,Math.log1p(sWatched)/Math.log1p(m.tvEpisodes))
          :(sWatched>0?1:0);
  const bump=Math.max(0,Number(cfg.TVBUMP)||0)*sf;
  const hist=Math.min(100,usage+rec+mu+bump)*w.h;
  const imdb=(Number.isFinite(m.rating)&&m.rating>0)
    ?Math.min(m.rating*10*voteConfidence(m.votes),100)*w.q:0;
  const ramp=SCORING.SHELF_RAMP_FULL_Q>0?Math.min(1,w.q/SCORING.SHELF_RAMP_FULL_Q):1;
  return hist+imdb+shelfPts*w.h*ramp;
}
const FILTER_REASONS={
  mov_off:'Movie cleanup is turned off in Cleanup scope — no movie is eligible',
  protected:'In a protected collection — never deleted',
  favorite:'A Jellyfin user favorited this movie',
  nodata:'No IMDb rating or votes found — not enough data to judge it',
  cutoff:'Above the IMDb rating cutoff',
  grace:'Within the grace period',
  unplayed:'Unplayed movies are skipped',
  tv_off:'TV cleanup is turned off in Cleanup scope — no season is eligible',
  tv_oos:'This series folder is not under a monitored directory, so cleanup will never touch it',
  tv_latest:'The latest season of a show not known to be ended — the household may be keeping up with it',
  tv_not_oldest:'Season eligibility is "only the oldest season" — this one waits its turn',
  tv_newest:'Season eligibility holds back the show\'s most recently added season',
  tv_bigseason:'More episodes than the season episode cap — a show filed under one season number, not a season',
};
function fmtV(n){return n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':n.toLocaleString();}
function fmtE(m){return m<1?'< 1 mo':m<2?'1 mo ago':m<12?Math.round(m)+' mo ago':m<24?'1 yr ago':Math.floor(m/12)+'+ yr ago';}
function scC(s){return s<10?'var(--text-danger)':s<40?'var(--text-warning)':s<80?'var(--text-secondary)':'var(--text-accent)';}
function scF(s){return s<10?'var(--bg-danger)':s<40?'var(--bg-warning)':s<80?'var(--bg-neutral)':'var(--bg-accent)';}
function retBarPct(s){return clamp(s,2,100);}
function deletionCompare(a,b){
  // Engine's _deletion_sort_key: score ascending; exact ties break
  // never-watched first, then (only when IMDb is in use) lowest IMDb rating
  // first, oldest added, larger files first, title.
  if(a.retention!==b.retention)return a.retention-b.retention;
  // Watched = any play evidence, count OR timestamp — same as the engine.
  const wa=(a.playCount>0||a.lastPlayedDays!=null)?1:0, wb=(b.playCount>0||b.lastPlayedDays!=null)?1:0;
  if(wa!==wb)return wa-wb;
  // Lowest IMDb rating deletes first; missing rating ranks highest (kept).
  // Disabled at 100% watch history (no cutoff) so ties stay pure oldest-first.
  if(imdbInUse()){
    const ra=(a.rating==null)?10:a.rating, rb=(b.rating==null)?10:b.rating;
    if(ra!==rb)return ra-rb;
  }
  // days-since-added: larger = older = deletes first (missing = oldest)
  const aa=(a.addedDays==null)?1e9:a.addedDays, ab=(b.addedDays==null)?1e9:b.addedDays;
  if(aa!==ab)return ab-aa;
  if((a.sizeGb||0)!==(b.sizeGb||0))return (b.sizeGb||0)-(a.sizeGb||0);
  return a.title.localeCompare(b.title);
}
function keyLess(a,b){
  for(let i=0;i<a.length;i++){
    const d=typeof a[i]==='string'?a[i].localeCompare(b[i]):a[i]-b[i];
    if(d)return d<0;
  }
  return false;
}
// Engine's _pop_from_tie_group: if any single member covers the remaining
// target, the SMALLEST one that covers it goes; else the largest first.
//
// Smallest-that-covers, not lowest-scoring-that-covers — this mirror had the
// two the other way round, which is the behaviour the engine deliberately
// stopped doing: everything in this group is near-tied, so the scores are
// equivalent by definition, and letting a fraction of a point decide sent a
// 42 GB file to cover a 2 GB need while a 4 GB file scoring one point higher
// survived. The preview named the 42 GB one; the run deleted the 4 GB one.
//
// No title tiebreak either, for the engine's reason: on an exact size+score
// tie the comparison is strict, so the EARLIEST member wins — and the group
// arrives in plan order, which is where the remaining tiebreaks (never-watched,
// lowest IMDb rating, oldest added) already live. Sorting by title here
// ordered them alphabetically, naming a movie that was not the one at the top
// of the queue.
function pickFromTieGroup(group,remainingGb){
  let bestI=-1,best=null;
  for(let i=0;i<group.length;i++){
    const c=group[i];
    if((c.sizeGb||0)>=remainingGb){
      const key=[c.sizeGb||0,c.retention];
      if(!best||keyLess(key,best)){best=key;bestI=i;}
    }
  }
  if(bestI<0){
    for(let i=0;i<group.length;i++){
      const c=group[i];
      const key=[-(c.sizeGb||0),c.retention];
      if(!best||keyLess(key,best)){best=key;bestI=i;}
    }
  }
  return group.splice(bestI,1)[0];
}
// Engine's _pop_next_deletion: strict score order until the movies near-tied
// with the head hold more space than what is left to free — only some of that
// group needs to go, so it activates and pickFromTieGroup takes over.
function popNextDeletion(pending,tieGroup,remainingGb){
  if(tieGroup.length){
    if(!(remainingGb>0))return tieGroup.shift();
    return pickFromTieGroup(tieGroup,remainingGb);
  }
  const win=tieWindow();
  if(win!=null&&remainingGb>0&&pending.length>1){
    const hs=pending[0].retention;
    let j=0,total=0;
    while(j<pending.length&&pending[j].retention-hs<=win){total+=pending[j].sizeGb||0;j++;}
    if(j>=2&&total>remainingGb){
      tieGroup.push(...pending.splice(0,j));
      return pickFromTieGroup(tieGroup,remainingGb);
    }
  }
  return pending.shift();
}
function deletionSequence(rows,targetGb){
  // Replay the engine's deletion loop over all eligible rows — the order a
  // run with this target would delete in. `boundary` is the one tie group
  // the target lands inside (the table's only bracket); the prune plan is
  // the prefix that covers the target.
  const pending=rows.filter(m=>m.status==='ok').slice().sort(deletionCompare);
  const tieGroup=[];
  const order=[];
  const boundary=new Set();
  const plan={ids:new Set(),freed:0,count:0,complete:targetGb<=0};
  let freed=0;
  while(pending.length||tieGroup.length){
    const groupWasEmpty=tieGroup.length===0;
    const m=popNextDeletion(pending,tieGroup,targetGb-freed);
    if(groupWasEmpty&&tieGroup.length){
      boundary.add(m.id);
      tieGroup.forEach(x=>boundary.add(x.id));
    }
    const pruned=targetGb>0&&freed<targetGb;
    freed+=m.sizeGb||0;
    order.push(m);
    if(pruned){plan.ids.add(m.id);plan.count++;plan.freed=freed;}
  }
  if(targetGb>0)plan.complete=plan.freed>=targetGb;
  return {order,plan,boundary};
}
function stackEligibleOn(){
  const el=document.getElementById('stack-eligible');
  return !!(el&&el.checked);
}
function eligibleGroupCompare(a,b){
  if(!stackEligibleOn())return 0;
  const ea=a.status==='ok',eb=b.status==='ok';
  if(ea!==eb)return ea?-1:1;
  return 0;
}
function statusSortText(m,plan){
  if(plan&&plan.ids&&plan.ids.has(m.id))return '0-pruned';
  if(m.status==='ok')return '1-eligible';
  return '2-filtered-'+m.status;
}
function typeLabel(m){return m.mediaType==='tv'?'TV Show · S'+m.seasonN:'Movie';}
function columnCompare(a,b,col,plan,rank){
  if(col==='title')return a.title.localeCompare(b.title);
  if(col==='type')return typeLabel(a).localeCompare(typeLabel(b))||a.title.localeCompare(b.title);
  // '#' = the real deletion order (strict score order plus the target-aware
  // pick where the over-headroom target lands in a near-tie group);
  // 'Retention' = the raw score alone.
  if(col==='order'){
    const ra=rank&&rank.get(a.id),rb=rank&&rank.get(b.id);
    if(ra!=null&&rb!=null)return ra-rb;
    if(ra!=null||rb!=null)return ra!=null?-1:1;
    return deletionCompare(a,b);
  }
  if(col==='score')return (a.retention-b.retention)||a.title.localeCompare(b.title);
  if(col==='status')return statusSortText(a,plan).localeCompare(statusSortText(b,plan));
  let va,vb;
  // Missing years are stored as the display string '—'; comparing that against
  // numbers is inconsistent (both orders return 1) and scrambles the sort.
  if(col==='year'){va=Number(a.year)||0;vb=Number(b.year)||0;}
  else if(col==='rating'){va=Number.isFinite(a.rating)?a.rating:-1;vb=Number.isFinite(b.rating)?b.rating:-1;}
  else if(col==='votes'){va=a.votes||0;vb=b.votes||0;}
  else if(col==='plays'){va=a.playCount||0;vb=b.playCount||0;}
  else if(col==='users'){va=a.users||0;vb=b.users||0;}
  else if(col==='size'){va=a.sizeGb;vb=b.sizeGb;}
  else if(col==='added'){va=a.addedDays??1e9;vb=b.addedDays??1e9;}
  else{va=(a.lastPlayedDays!=null)?a.lastPlayedDays:1e9;vb=(b.lastPlayedDays!=null)?b.lastPlayedDays:1e9;}
  return va===vb?0:va<vb?-1:1;
}
function renderT(){
  const cfg=gcfg();
  if(!poolState.loaded){
    const msg=htmlEsc(poolState.message||'Loading library…');
    // The table is wider than the phone screen, so a plain centered cell would
    // center across the swipe width. Pin the message to the scroll viewport
    // instead: a sticky span sized to the wrapper stays centered in view.
    const tbody=document.getElementById('mtbody');
    const wrapW=Math.max(0,(tbody.closest('.twrap')?.clientWidth||0)-2);
    const inner=poolState.scanning?`<span class="pending-ellipsis">${msg}</span>`:msg;
    tbody.innerHTML=
      `<tr><td colspan="13" style="text-align:left;padding:1.1rem 0"><span class="tbl-msg" style="width:${wrapW}px">${inner}</span></td></tr>`;
    document.getElementById('tstat').textContent='';
    const pg0=document.getElementById('tbl-pager');
    if(pg0)pg0.hidden=true;
    const ps0=document.getElementById('prunestat');
    if(ps0){ps0.textContent='';ps0.className='prunestat';}
    return;
  }
  // The type filter narrows what is scored AND ranked: a Movies-only view
  // shows the deletion order a movie run would use, not a mixed ranking with
  // gaps where the other type sat.
  const typeFilter=document.getElementById('type-filter')?.value||'all';
  const source=typeFilter==='all'?raw:raw.filter(m=>m.mediaType===typeFilter);
  if(!source.length){
    const tbody=document.getElementById('mtbody');
    const wrapW=Math.max(0,(tbody.closest('.twrap')?.clientWidth||0)-2);
    const emptyMsg=typeFilter==='tv'
      ?'No TV seasons in the library database.'
      :'No movies in the library database.';
    tbody.innerHTML=
      `<tr><td colspan="13" style="text-align:left;padding:1.1rem 0"><span class="tbl-msg" style="width:${wrapW}px">${htmlEsc(emptyMsg)}</span></td></tr>`;
    document.getElementById('tstat').textContent='';
    const pgE=document.getElementById('tbl-pager');
    if(pgE)pgE.hidden=true;
    const psE=document.getElementById('prunestat');
    if(psE){psE.textContent='';psE.className='prunestat';}
    return;
  }
  // Score the whole filtered set — ranking, prune plan, and stats span it;
  // only the on-screen rows are paginated below.
  let rows=source.map(m=>{
    const r=scoreOne(m,cfg);
    // TV rows are seasons IN the pool: scored on the unified scale and run
    // through the season eligibility ladder, so an unfiltered season ranks
    // in the SAME deletion order (and prune plan) as the movies — the merged
    // order the daily passes split between the two executors.
    if(m.mediaType==='tv'){r.retention=seasonScore(m,cfg);r.status=seasonStatus(m,cfg);}
    return r;
  });
  // # rank = the exact order a run with the current over-headroom target
  // would delete in.
  const seq=deletionSequence(rows,appliedPruneGb);
  const plan=seq.plan;
  const rank=new Map();
  seq.order.forEach((m,i)=>rank.set(m.id,i+1));
  rows.sort((a,b)=>{
    const groupCmp=eligibleGroupCompare(a,b);
    if(groupCmp)return groupCmp;
    const cmp=columnCompare(a,b,sc,plan,rank);
    return sd*cmp;
  });
  const elig=rows.filter(m=>m.status==='ok').length;
  const filtered=rows.length-elig;
  const tstat=document.getElementById('tstat');
  const noun=typeFilter==='tv'?'seasons':typeFilter==='movie'?'movies':'titles';
  tstat.textContent=rows.length+' '+noun+' · '+elig+' eligible · '+filtered+' filtered';
  // An unrated snapshot previewed against IMDb-weighted scoring filters every
  // row as "no IMDb data" — explain that the next run annotates ratings (it
  // downloads the dataset itself when scoring needs it).
  const note=document.getElementById('snapshot-imdb-note');
  if(note){
    const unrated=raw.length>0&&raw.every(m=>!(Number.isFinite(m.rating)&&m.rating>0));
    if(unrated&&imdbInUse(cfg)){
      note.style.display='';
      note.textContent='This snapshot has no IMDb ratings — the last run was scored on watch history alone. '
        +'Save an IMDb-weighted balance (or a rating cutoff) and run Simulate to annotate ratings.';
    }else{
      note.style.display='none';
    }
  }
  const ps=document.getElementById('prunestat');
  if(ps){
    if(appliedPruneGb>0){
      ps.textContent=plan.count+' row'+(plan.count===1?'':'s')+' marked · '+plan.freed.toFixed(1)+' / '+appliedPruneGb.toFixed(0)+' GB'+(plan.complete?'':' · not enough eligible space');
      ps.className='prunestat active';
    }else{
      ps.textContent='No target set';ps.className='prunestat';
    }
  }
  // One bracket: the tie group the target lands inside, drawn only while
  // sorted by # (its members are contiguous in that order).
  const tieMarks=new Array(rows.length).fill('');
  if(sc==='order'&&seq.boundary.size>=2){
    let i0=-1,i1=-1;
    rows.forEach((m,i)=>{if(seq.boundary.has(m.id)){if(i0<0)i0=i;i1=i;}});
    if(i1>i0){
      tieMarks[i0]='tie-start';
      for(let k=i0+1;k<i1;k++)tieMarks[k]='tie-mid';
      tieMarks[i1]='tie-end';
    }
  }
  // Paged rendering: everything above (rank, prune plan, stats, tie group) is
  // computed over the FULL library; only the visible slice is rendered. The
  // pager stays visible whenever a smaller page size could paginate, so the
  // size dropdown never strands itself hidden.
  const pages=Math.max(1,Math.ceil(rows.length/tblPageSize));
  tablePage=Math.min(Math.max(0,tablePage),pages-1);
  const pager=document.getElementById('tbl-pager');
  if(pager){
    pager.hidden=rows.length<=10;
    const prev=document.getElementById('tbl-prev'),next=document.getElementById('tbl-next');
    if(prev)prev.disabled=tablePage<=0;
    if(next)next.disabled=tablePage>=pages-1;
    const lbl=document.getElementById('tbl-page-label');
    if(lbl){
      const s0=tablePage*tblPageSize+1,s1=Math.min((tablePage+1)*tblPageSize,rows.length);
      lbl.textContent=s0.toLocaleString()+'–'+s1.toLocaleString()+' of '+rows.length.toLocaleString();
    }
  }
  const pageStart=tablePage*tblPageSize;
  document.getElementById('mtbody').innerHTML=rows.slice(pageStart,pageStart+tblPageSize).map((m,pi)=>{
    const i=pageStart+pi;
    const pruned=plan.ids.has(m.id);
    const bh=pruned
      ?`<span class="table-status marked">marked</span>`
      :(m.status==='ok'
        ?`<span class="table-status ok">eligible</span>`
        :`<span class="table-status filtered" title="${htmlEsc(FILTER_REASONS[m.status]||'Filtered')}">filtered — ${htmlEsc(FILTER_LABELS[m.status]||'')}</span>`);
    return`<tr class="movie-row ${selectedMovieId===m.id?'selected ':''}${m.status!=='ok'?'fx ':''}${pruned?'prune ':''}${tieMarks[i]}" data-mid="${htmlEsc(m.id)}" tabindex="0" role="button" aria-label="Load movie facts for ${htmlEsc(m.title)}" onclick="selectMovieById(this.dataset.mid)" onkeydown="selectMovieOnKey(event,this.dataset.mid)">
<td class="ordcell">${m.status==='ok'?rank.get(m.id):'—'}</td>
<td style="max-width:175px;overflow:hidden;text-overflow:ellipsis">${htmlEsc(m.title)}</td>
<td style="color:var(--text-secondary);white-space:nowrap">${typeLabel(m)}</td>
<td style="color:var(--text-secondary)">${m.year}</td>
<td>${Number.isFinite(m.rating)?m.rating.toFixed(1):'—'}</td>
<td style="color:var(--text-secondary)">${m.votes>0?fmtV(m.votes):'—'}</td>
<td style="color:var(--text-secondary)">${m.playCount||0}</td>
<td style="color:var(--text-secondary)">${m.users||0}</td>
<td style="color:var(--text-secondary)">${m.lastPlayedDays!=null?fmtE(m.lastPlayedDays/30):'never'}</td>
<td style="color:var(--text-secondary)">${m.addedDays!=null?fmtE(m.addedDays/30):'—'}</td>
<td style="color:var(--text-secondary)">${m.sizeGb.toFixed(1)} GB</td>
<td><div class="scbar"><div class="scfill" style="width:${retBarPct(m.retention).toFixed(0)}%;background:${scF(m.retention)}"></div><div class="sctxt" style="color:${scC(m.retention)}">${m.retention.toFixed(1)}</div></div></td>
<td>${bh}</td></tr>`;
  }).join('');
}
function setSingleMovieFromMovie(m){
  if(!m)return;
  const set=(id,val)=>{
    const el=document.getElementById(id);
    if(el)el.value=val;
  };
  set('s-rat',Number.isFinite(m.rating)?clamp(m.rating,0,10):0);
  set('s-vot',sliderValueFromVotes(m.votes));
  set('s-play',clamp(m.playCount||0,0,20));
  // Slider = the movie's recency age on one timeline: last-watched if played,
  // else its added date — so a clicked movie reproduces its real recency + shelf.
  set('s-lp',clamp(Math.round(m.lastPlayedDays!=null?m.lastPlayedDays:(m.addedDays!=null?m.addedDays:60)),0,2920));
  set('s-usr',clamp(m.users||0,0,5));
  go();
}
function selectMovieById(id){
  const m=raw.find(x=>x.id===id);
  if(!m)return;
  // renderT() rebuilds tbody.innerHTML, which drops keyboard focus to <body>
  // — a keyboard user would have to Tab back through the whole page after
  // every selection. Restore focus to the same row in the fresh DOM.
  const hadFocus=!!(document.activeElement&&document.activeElement.classList
    &&document.activeElement.classList.contains('movie-row'));
  selectedMovieId=id;
  setSingleMovieFromMovie(m);
  renderT();
  if(hadFocus)document.querySelector(`.movie-row[data-mid="${CSS.escape(id)}"]`)?.focus();
}
function selectMovieOnKey(e,id){
  if(e.key==='Enter'||e.key===' '){
    e.preventDefault();
    selectMovieById(id);
  }
}
function sb(col){
  if(sc===col)sd*=-1;else{sc=col;sd=1;}
  tablePage=0;
  const map={order:'th-o',title:'th-t',type:'th-m',year:'th-y',rating:'th-r',votes:'th-v',plays:'th-p',users:'th-u',eng:'th-e',added:'th-a',size:'th-z',score:'th-s',status:'th-x'};
  const nm={order:'#',title:'Title',type:'Type',year:'Year',rating:'IMDB',votes:'Votes',plays:'Plays',users:'Users',eng:'Last watched',added:'Added',size:'Size',score:'Retention',status:'Eligibility'};
  Object.keys(map).forEach(k=>{
    const el=document.getElementById(map[k]);
    el.className=sc===k?'sc':'';
    el.textContent=nm[k]+(sc===k?(sd===1?' ↑':' ↓'):'');
  });
  renderT();
}
function syncMetricRangeFill(el){
  const min=Number(el.min||0),max=Number(el.max||100),val=Number(el.value||0);
  const pct=max>min?clamp(((val-min)/(max-min))*100,0,100):0;
  el.style.setProperty('--range-progress',pct+'%');
}
function syncMetricRangeFills(){
  document.querySelectorAll('.metric-range').forEach(syncMetricRangeFill);
}
// ── Score composition pie ────────────────────────────────────────────────────
// One 0–100 circle in three parts: earned watch-history, earned IMDb, and the
// unearned remainder (the blank circle underneath). Wedges run clockwise from
// 12 o'clock; the viewBox lets the pie scale with its container.
function _pieWedgePath(cx,cy,r,startFrac,endFrac){
  const f0=Math.min(startFrac,endFrac), f1=Math.max(startFrac,endFrac);
  const span=f1-f0;
  if(span<=0.0005)return '';
  if(span>=0.9995){ // full circle: two half-arcs (a single 360° arc collapses)
    return `M ${cx} ${cy-r} A ${r} ${r} 0 1 1 ${cx} ${cy+r} A ${r} ${r} 0 1 1 ${cx} ${cy-r} Z`;
  }
  const a0=(f0*360-90)*Math.PI/180, a1=(f1*360-90)*Math.PI/180;
  const x0=cx+r*Math.cos(a0), y0=cy+r*Math.sin(a0);
  const x1=cx+r*Math.cos(a1), y1=cy+r*Math.sin(a1);
  const large=span>0.5?1:0;
  return `M ${cx} ${cy} L ${x0.toFixed(3)} ${y0.toFixed(3)} A ${r} ${r} 0 ${large} 1 ${x1.toFixed(3)} ${y1.toFixed(3)} Z`;
}
function _renderScorePie(histPts,imdbPts){
  const h=clamp(histPts,0,100)/100, q=clamp(imdbPts,0,100)/100;
  const earned=Math.min(1,h+q);
  // Center the earned (colored) wedges on the LEFT (9 o'clock = 0.75), so the
  // unearned gap sits symmetrically on the right.
  const start=0.75-earned/2;
  const hp=document.getElementById('pie-hist'), qp=document.getElementById('pie-imdb');
  if(hp)hp.setAttribute('d',_pieWedgePath(100,100,90,start,start+h));
  if(qp)qp.setAttribute('d',_pieWedgePath(100,100,90,start+h,start+earned));
  const t=(id,txt)=>{const el=document.getElementById(id);if(el)el.textContent=txt;};
  t('pie-hist-title','Watch history: +'+histPts.toFixed(1)+' of 100');
  t('pie-imdb-title','IMDb: +'+imdbPts.toFixed(1)+' of 100');
  t('pie-blank-title','Unearned: '+Math.max(0,100-histPts-imdbPts).toFixed(1)+' of 100');
}

function go(){
  syncMetricRangeFills();
  const cfg=gcfg();
  const plays=+document.getElementById('s-play').value;
  const lpDays=+document.getElementById('s-lp').value;
  const users=+document.getElementById('s-usr').value;
  const rat=+document.getElementById('s-rat').value;
  const votes=voteFromSlider();

  document.getElementById('v-play').textContent=plays;
  document.getElementById('v-lp').textContent=(plays>0?'watched ':'added ')+(lpDays===0?'today':fmtE(lpDays/30));
  document.getElementById('v-usr').textContent=users;
  document.getElementById('v-rat').textContent=rat>0?rat.toFixed(1):'unrated';
  document.getElementById('v-vot').textContent=votes?fmtV(votes):'0';
  const conf=voteConfidence(votes>0?votes:0);
  document.getElementById('v-conf').textContent=rat>0
    ?'Vote confidence: '+(conf*100).toFixed(0)+'% — votes back up the rating, they never protect on their own'
    :'Unrated movies earn no IMDb points.';

  // One recency timeline: the slider is last-watched when played, or the added
  // date when never played (added_at and last_played are the same input here).
  const m={playCount:plays,lastPlayedDays:plays>0?lpDays:null,addedDays:lpDays,users,
           rating:rat>0?rat:null,votes};
  const{breakdown:b,retention}=retentionBreakdown(m,cfg);
  const w=balanceWeights(cfg.BAL);
  const hPct=Math.round(w.h*100),qPct=Math.round(w.q*100);

  const hist=b.usage+b.recency+b.multi_user+b.shelf;
  document.getElementById('v-hist').textContent='+'+hist.toFixed(1);
  document.getElementById('v-hist-weight').textContent=hPct+'% of the score';
  document.getElementById('v-hist-detail').textContent=
    'frequency '+b.usage.toFixed(1)+' · recency '+b.recency.toFixed(1)+' · users '+b.multi_user.toFixed(1)
    +(b.shelf>0.05?' · soft shelf '+b.shelf.toFixed(1):'')
    +' — out of '+hPct+' max at this balance';

  document.getElementById('v-imdb').textContent='+'+b.imdb.toFixed(1);
  document.getElementById('v-imdb-weight').textContent=qPct+'% of the score';
  document.getElementById('v-imdb-detail').textContent=
    rat>0?('rating '+rat.toFixed(1)+' × 10 × confidence '+(conf*100).toFixed(0)+'% × weight '+qPct+'% — out of '+qPct+' max at this balance')
         :'unrated — no IMDb points';

  const blank=Math.max(0,100-retention);
  document.getElementById('v-blank').textContent=blank.toFixed(1);
  _renderScorePie(hist,b.imdb);

  document.getElementById('v-mix').textContent='history '+hPct+'% + IMDb '+qPct+'%';

  document.getElementById('v-overall').textContent=retention.toFixed(1);
  const ve=document.getElementById('v-verd');
  const V=retention<10?['First to go','var(--text-danger)','var(--bg-danger)','var(--border-danger)']:
           retention<40?['Deletion target','var(--text-warning)','var(--bg-warning)','var(--border-warning)']:
           retention<80?['Mid-range','var(--text-secondary)','var(--surface-0)','var(--border)']:
           ['Kept longest','var(--text-accent)','var(--bg-accent)','var(--border-accent)'];
  ve.textContent=V[0];ve.style.color=V[1];ve.style.background=V[2];ve.style.borderColor=V[3];
}
function all(){go();renderT();}
// poolImdbOnDisk: an IMDb dataset exists server-side — the unrated-snapshot
// note uses it to explain what the next run will do.
let poolImdbOnDisk=false;
// Library table pagination over the full snapshot (25/page default).
let tblPageSize=25;
let tablePage=0;
function tablePageStep(delta){tablePage+=delta;renderT();}
function tblSetPageSize(v){tblPageSize=Math.max(1,parseInt(v,10)||25);tablePage=0;renderT();}
async function loadPool(){
  selectedMovieId=null;
  poolState={loaded:false,message:'Loading library…'};
  renderT();
  try{
    const d=await fetch('/api/library-snapshot?_='+Date.now(),{cache:'no-store'}).then(r=>r.json());
    if(!d||!d.ok){
      raw=[];
      poolState={loaded:false,message:(d&&d.message)||'No library snapshot available — run a Simulate to build it.'};
      renderT();
      return;
    }
    const now=Date.now()/1000;
    raw=(d.movies||[]).flatMap((m,i)=>{
      const base={
        id:'m'+i+'-'+String(m.title||''),
        title:m.title||'—',
        year:m.year||'—',
        rating:(Number.isFinite(m.rating)&&m.rating>0)?m.rating:null,
        votes:m.votes||0,
        playCount:m.plays||0,
        users:m.users||0,
        lastPlayedDays:(m.last_played>0)?Math.max(0,(now-m.last_played)/86400):null,
        addedDays:(m.added_at>0)?Math.max(0,(now-m.added_at)/86400):null,
        sizeGb:+(m.size_gb||0),
        protected:!!m.protected,
        favorite:!!m.favorite,
        // Anything not explicitly 'tv' is a movie, matching the store's default.
        mediaType:(m.media_type==='tv')?'tv':'movie',
        tvStatus:m.tv_status||null,
        tvEpisodes:m.tv_episodes||0,
        tvEpisodesWatched:m.tv_episodes_watched||0,
        // Truthy-only, matching the plan: a row that somehow reaches the
        // snapshot unstamped must preview as off-path, not as deletable.
        tvInScope:!!m.tv_in_scope,
      };
      if(base.mediaType!=='tv')return[base];
      // The season is the TV unit: one row per season on disk, each scored on
      // the movie 0-100 scale, so seasons and movies read as ONE pool. A
      // series row with no per-season facts contributes nothing (there is no
      // season to delete).
      const seasons=(m.tv_seasons||[]).filter(s=>s&&typeof s==='object');
      if(!seasons.length)return[];
      const latestN=Math.max(...seasons.map(s=>s.n||0));
      const oldestN=Math.min(...seasons.map(s=>s.n||0));
      // "Newest" for the season-eligibility rule = most recently ADDED (by
      // each season's own date, highest number as the tiebreak) — which may
      // not be the latest season.
      let newest=seasons[0];
      for(const s of seasons){
        if((s.added_at||0)>(newest.added_at||0)
           ||((s.added_at||0)===(newest.added_at||0)&&(s.n||0)>(newest.n||0)))newest=s;
      }
      return seasons.map(s=>({
        ...base,
        id:base.id+'-s'+s.n,
        // The Type column names the season ("TV Show · S2"); the title stays
        // the show's plain name.
        sizeGb:+(((s.size_bytes||0))/1e9),
        seasonN:s.n,
        seasonEps:s.eps||0,
        seasonEpsWatched:s.eps_watched||0,
        // Rows stored before seasons carried plays/users fall back to the
        // nearest older fact, matching season_retention_score.
        seasonPlays:(s.plays!=null)?s.plays:(s.eps_watched||0),
        seasonUsers:(s.users!=null)?s.users:(m.users||0),
        seasonLastPlayedDays:(s.last_played>0)?Math.max(0,(now-s.last_played)/86400):null,
        seasonAddedDays:(s.added_at>0)?Math.max(0,(now-s.added_at)/86400):null,
        // The latest season is shielded unless the show is KNOWN ended —
        // Plex states no status, and unknown gets the benefit of the doubt.
        latestOfContinuing:(m.tv_status!=='ended')&&((s.n||0)===latestN),
        isOldestSeason:(s.n||0)===oldestN,
        isNewestSeason:(s.n||0)===(newest.n||0),
      }));
    });
    poolImdbOnDisk=!!d.imdb_dataset_on_disk;
    poolState={loaded:true,message:''};
  }catch(e){
    raw=[];
    poolState={loaded:false,message:'Could not load the library snapshot.'};
  }
  renderT();
}
appliedCfg=readCfg();
loadPool();go();
updateCfgButtons();
_applyExplorerRunLock();   // server-rendered run state; the status poll keeps it live
// Sortable column headers are click-only in the markup — make them reachable
// and operable from the keyboard too.
document.querySelectorAll('thead th[onclick]').forEach(th=>{
  th.tabIndex=0;
  th.setAttribute('role','button');
  th.addEventListener('keydown',e=>{
    if(e.key==='Enter'||e.key===' '){e.preventDefault();th.click();}
  });
});

// Sync saved config values into the explorer inputs, then render with the current config.
(function() {
  const cfg = expBootConfig;
  expLastCutoff = cfg._MAX_IMDB_RATING_LAST ?? null;
  expLastTie = cfg._NEAR_TIE_PTS_LAST ?? null;
  savedCfg = cfgFromServer(cfg);
  setFormFromCfg(savedCfg);
  appliedCfg = canonCfg(savedCfg);
  if (typeof all === 'function') all();
  updateCfgButtons();
})();
