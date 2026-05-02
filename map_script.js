
let view, graphicsLayer, Graphic, Point;
let allComps = [], allMappedComps = [], filteredMappedComps = [], activeGraphics = [];
let pinEditComp = null;
const LOCAL_DEALS_KEY='elc_manual_added_deals_v1';
const LOCAL_EDITS_KEY='elc_comp_edits_v1';
const PIN_OVERRIDES_KEY='elc_pin_location_overrides_v1';
let editingCompKey = null;
const FIELD_LABELS = {
  num:'#',parcel_id:'Parcel ID',parcel_raw_id:'Parcel Raw ID',address:'Address',sale_date:'Sale Date',
  price:'Price',acreage:'Acres',price_per_acre:'$/Acre',book_page:'Book/Page',transaction_type:'Transaction Type',
  grantor:'Seller',grantee:'Buyer',deed_type:'Deed Type',property_type:'Property Type',zoning:'Zoning',
  land_use:'Land Use',source:'Source',comments:'Comments',lat:'Latitude',lon:'Longitude',
  coords_approx:'Coordinates Approximate',deed_url:'Deed URL',aerial:'Aerial',sale_date_iso:'Sale Date ISO',unit_count:'Unit Count',price_per_unit:'$/Unit',elc_deal:'ELC Deal'
};
const hasValue = v => v !== null && v !== undefined && v !== '';
const escapeHtml = s => String(s ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const fmtPrice = n => hasValue(n) ? '$' + Math.round(Number(n)).toLocaleString() : 'N/A';
const fmtAcres = n => hasValue(n) ? Number(n).toLocaleString(undefined,{maximumFractionDigits:2}) + ' ac' : 'N/A';
const fmtPPA = n => hasValue(n) ? '$' + Math.round(Number(n)).toLocaleString() + '/ac' : null;
const fmtStr = s => hasValue(s) ? escapeHtml(s) : null;
const fmtLink = (url,label='View Aerial') => hasValue(url) ? `<a class="aerial-btn" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation();">${label}</a>` : null;
const parseCompDate = c => {
  const raw = c.sale_date_iso || c.sale_date;
  if(!hasValue(raw)) return null;
  const d = new Date(raw);
  return Number.isNaN(d.getTime()) ? null : d;
};

const AERIAL_FIELDS = ['aerial','photo_url','photoUrl','photoURL','Photo URL','PHOTO URL','photo url','photo','Photo','image_url','imageUrl','image','aerial_url','aerialUrl','Aerial URL','AERIAL URL','Aerial'];
function getAerialValue(c){ for(const field of AERIAL_FIELDS){ if(c && hasValue(c[field])) return c[field]; } return null; }
function normalizeComp(c){ const aerial=getAerialValue(c); return aerial && !hasValue(c.aerial) ? {...c,aerial} : c; }
function compMatchKeys(c){ const keys=[]; if(hasValue(c.num)) keys.push('num:'+String(c.num)); if(hasValue(c.address)) keys.push('addr:'+String(c.address).trim().toLowerCase()+'|date:'+String(c.sale_date||c.sale_date_iso||'').trim().toLowerCase()+'|price:'+String(c.price||'')); return keys; }
function mergeAerialsFromBackup(comps, backupComps){
  const aerialByKey=new Map();
  (backupComps||[]).map(normalizeComp).forEach(b=>{ const aerial=getAerialValue(b); if(!hasValue(aerial)) return; compMatchKeys(b).forEach(k=>{ if(!aerialByKey.has(k)) aerialByKey.set(k,aerial); }); });
  return (comps||[]).map(c=>{ let out=normalizeComp(c); if(hasValue(out.aerial)) return out; for(const k of compMatchKeys(out)){ if(aerialByKey.has(k)) return {...out,aerial:aerialByKey.get(k)}; } return out; });
}
async function loadAerialBackup(){ for(const file of ['./comps_aerial_backup.json','./comps_updated.json','./comps_with_aerials.json']){ try{ const res=await fetch(file,{cache:'no-store'}); if(res.ok) return await res.json(); }catch(e){} } return null; }

function loadLocalDeals(){ try{return JSON.parse(localStorage.getItem(LOCAL_DEALS_KEY) || '[]');}catch(e){return [];} }
function loadPinOverrides(){ try{return JSON.parse(localStorage.getItem(PIN_OVERRIDES_KEY) || '{}');}catch(e){return {};} }
function savePinOverrides(overrides){ localStorage.setItem(PIN_OVERRIDES_KEY, JSON.stringify(overrides)); }
function getCompKey(c, fallbackIndex){
  if(hasValue(c._compKey)) return c._compKey;
  if(hasValue(c._manual_id)) return 'manual:' + String(c._manual_id);
  if(hasValue(c.num)) return 'num:' + String(c.num);
  return 'fallback:' + String(fallbackIndex);
}

function loadLocalEdits(){ try{return JSON.parse(localStorage.getItem(LOCAL_EDITS_KEY) || '{}');}catch(e){return {};} }
function saveLocalEdits(edits){ localStorage.setItem(LOCAL_EDITS_KEY, JSON.stringify(edits)); }
function toNumOrNull(v){ if(v === null || v === undefined || String(v).trim()==='') return null; const n=Number(v); return Number.isFinite(n) ? n : null; }
function isoToDisplay(iso){ if(!iso) return null; const d=new Date(iso+'T00:00:00'); if(Number.isNaN(d.getTime())) return null; return d.toLocaleDateString('en-US',{month:'short',day:'2-digit',year:'numeric'}).replace(',', ''); }
function deriveCompFields(c){
  if(hasValue(c.price) && hasValue(c.acreage) && Number(c.acreage)!==0) c.price_per_acre=Math.round(Number(c.price)/Number(c.acreage)); else c.price_per_acre=null;
  if(hasValue(c.price) && hasValue(c.unit_count) && Number(c.unit_count)!==0) c.price_per_unit=Math.round(Number(c.price)/Number(c.unit_count)); else c.price_per_unit=null;
  if(c.sale_date_iso && !c.sale_date) c.sale_date=isoToDisplay(c.sale_date_iso);
  return c;
}
function applyLocalEdits(comps){
  const edits=loadLocalEdits();
  return (comps||[]).map((c,i)=>{ const key=getCompKey(c,i); const edited=edits[key] ? {...c,...edits[key],edited_locally:true} : c; return deriveCompFields({...edited,_compKey:key}); });
}
function saveCompEdit(comp){ if(!comp||!comp._compKey) return; const edits=loadLocalEdits(); edits[comp._compKey]={...comp,_compKey:undefined}; saveLocalEdits(edits); }
function setEditValue(id,val){ const el=document.getElementById(id); if(el) el.value=hasValue(val)?val:''; }
function fillEditForm(c){
  setEditValue('edit-property-type',c.property_type||'Land'); setEditValue('edit-transaction-type',c.transaction_type||'Sale'); setEditValue('edit-sale-date',c.sale_date_iso||''); setEditValue('edit-elc-deal',c.elc_deal==='Yes'?'Yes':'No');
  setEditValue('edit-address',c.address); setEditValue('edit-price',c.price); setEditValue('edit-acreage',c.acreage); setEditValue('edit-unit-count',c.unit_count); setEditValue('edit-lat',c.lat); setEditValue('edit-lon',c.lon);
  setEditValue('edit-grantee',c.grantee); setEditValue('edit-grantor',c.grantor); setEditValue('edit-zoning',c.zoning); setEditValue('edit-land-use',c.land_use); setEditValue('edit-source',c.source); setEditValue('edit-aerial-url',c.aerial); setEditValue('edit-deed-url',c.deed_url); setEditValue('edit-comments',c.comments);
}
window.openEditDealModal=function(key){ const comp=getCompByKey(key); if(!comp){alert('Could not find that comp to edit.'); return;} editingCompKey=key; fillEditForm(comp); document.getElementById('edit-deal-modal').classList.add('open'); if(view&&view.popup) view.popup.close(); };
window.closeEditDealModal=function(){ editingCompKey=null; document.getElementById('edit-deal-modal').classList.remove('open'); };
window.saveEditedDealFromMapForm=function(){
  const existing=getCompByKey(editingCompKey); if(!existing){alert('Could not find that comp to edit.'); return;}
  const saleIso=document.getElementById('edit-sale-date').value||null;
  let updated={...existing,address:document.getElementById('edit-address').value.trim()||null,sale_date:isoToDisplay(saleIso),sale_date_iso:saleIso,price:toNumOrNull(document.getElementById('edit-price').value),acreage:toNumOrNull(document.getElementById('edit-acreage').value),transaction_type:document.getElementById('edit-transaction-type').value.trim()||'Sale',grantor:document.getElementById('edit-grantor').value.trim()||null,grantee:document.getElementById('edit-grantee').value.trim()||null,property_type:document.getElementById('edit-property-type').value||'Land',zoning:document.getElementById('edit-zoning').value.trim()||null,land_use:document.getElementById('edit-land-use').value.trim()||null,source:document.getElementById('edit-source').value.trim()||null,comments:document.getElementById('edit-comments').value.trim()||null,lat:toNumOrNull(document.getElementById('edit-lat').value),lon:toNumOrNull(document.getElementById('edit-lon').value),coords_approx:false,deed_url:document.getElementById('edit-deed-url').value.trim()||null,aerial:document.getElementById('edit-aerial-url').value.trim()||null,unit_count:toNumOrNull(document.getElementById('edit-unit-count').value),elc_deal:document.getElementById('edit-elc-deal').value==='Yes'?'Yes':null,edited_locally:true};
  updated=deriveCompFields(updated); saveCompEdit(updated); allComps=allComps.map(c=>c._compKey===editingCompKey?updated:c); allComps=applyPinOverrides(applyLocalEdits(allComps)); allMappedComps=allComps.filter(c=>hasValue(c.lat)&&hasValue(c.lon)).map((c,i)=>({...c,_mappedIndex:i})); closeEditDealModal(); populateFilters(); applyFilters(); alert('Changes saved in this browser.');
};

function applyPinOverrides(comps){
  const overrides = loadPinOverrides();
  return comps.map((c,i)=>{
    const key = getCompKey(c,i);
    const override = overrides[key];
    return override ? {...c, _compKey:key, lat:override.lat, lon:override.lon, coords_approx:false, pin_edited:true} : {...c, _compKey:key};
  });
}
function getCompByKey(key){ return allComps.find(c=>c._compKey === key) || allMappedComps.find(c=>c._compKey === key); }
function getPriceColor(price){
  if(!hasValue(price)) return [170,170,170,220];
  price=Number(price);
  if(price<500000) return [66,153,225,235];
  if(price<1000000) return [72,187,120,235];
  if(price<5000000) return [237,137,54,235];
  if(price<10000000) return [229,62,62,235];
  return [128,90,213,235];
}
function getAcreSize(a){
  if(!hasValue(a)) return 7; a=Number(a);
  if(a<1) return 6; if(a<5) return 9; if(a<20) return 13; if(a<50) return 17; return 22;
}
function buildPopupContent(comp){
  const rows=[
    ['Price',fmtPrice(comp.price)],['Acres',fmtAcres(comp.acreage)],['$/Acre',fmtPPA(comp.price_per_acre)],
    ['Units',hasValue(comp.unit_count) ? Number(comp.unit_count).toLocaleString() : null],['$/Unit',fmtPPA(comp.price_per_unit)],
    ['Sale Date',fmtStr(comp.sale_date)],['Type',fmtStr(comp.property_type)],['ELC Deal',fmtStr(comp.elc_deal)],['Zoning',fmtStr(comp.zoning)],
    ['Land Use',fmtStr(comp.land_use)],['Seller',fmtStr(comp.grantor)],['Buyer',fmtStr(comp.grantee)],
    ['Source',fmtStr(comp.source)],['Aerial',fmtLink(comp.aerial,'View Aerial')],['Deed',fmtLink(comp.deed_url,'View Deed')]
  ].filter(r=>r[1]!=null);
  const wrap = document.createElement('div');
  const table = document.createElement('table');
  table.className = 'popup-table';
  rows.forEach(([k,v])=>{ const tr=document.createElement('tr'); const tdK=document.createElement('td'); const tdV=document.createElement('td'); tdK.textContent=k; tdV.innerHTML=v; tr.appendChild(tdK); tr.appendChild(tdV); table.appendChild(tr); });
  const trPin=document.createElement('tr'); const tdPinK=document.createElement('td'); const tdPinV=document.createElement('td');
  tdPinK.textContent='Pin Location';
  const btn=document.createElement('button'); btn.className='pin-edit-link'; btn.type='button'; btn.textContent='Change Pin';
  btn.setAttribute('data-pin-key', comp._compKey || '');
  btn.addEventListener('click', function(e){ e.preventDefault(); e.stopPropagation(); if(view && view.popup) view.popup.close(); window.startPinEdit(comp._compKey); });
  tdPinV.appendChild(btn); trPin.appendChild(tdPinK); trPin.appendChild(tdPinV); table.appendChild(trPin); const trEdit=document.createElement('tr'); const tdEditK=document.createElement('td'); const tdEditV=document.createElement('td'); tdEditK.textContent='Fields'; const editBtn=document.createElement('button'); editBtn.className='edit-btn'; editBtn.type='button'; editBtn.textContent='Edit Fields'; editBtn.addEventListener('click',function(e){e.preventDefault();e.stopPropagation();window.openEditDealModal(comp._compKey);}); tdEditV.appendChild(editBtn); trEdit.appendChild(tdEditK); trEdit.appendChild(tdEditV); table.appendChild(trEdit); wrap.appendChild(table);
  const comments=fmtStr(comp.comments);
  if(comments){ const div=document.createElement('div'); div.style.marginTop='8px'; div.style.fontSize='12px'; div.style.lineHeight='1.35'; div.innerHTML='<strong>Comments:</strong> '+comments; wrap.appendChild(div); }
  return wrap;
}
function buildListCard(comp,idx){
  const ppa=fmtPPA(comp.price_per_acre);
  const type=comp.property_type ? `<span class="comp-tag">${escapeHtml(comp.property_type)}</span>` : '';
  const date=comp.sale_date ? `<span class="comp-tag">${escapeHtml(comp.sale_date)}</span>` : '';
  const aerial=comp.aerial ? `<span class="comp-tag comp-tag-red">Aerial</span>` : '';
  const elc=comp.elc_deal==='Yes' ? `<span class="comp-tag comp-tag-red">ELC Deal</span>` : '';
  const units=hasValue(comp.unit_count) ? `<span class="comp-tag">${Number(comp.unit_count).toLocaleString()} Units</span>` : '';
  const sel=fmtStr(comp.grantor), buy=fmtStr(comp.grantee);
  const parties=(sel||buy) ? `<div class="comp-parties">${sel||'N/A'} to ${buy||'N/A'}</div>` : '';
  const links=`<div class="comp-links">${comp.aerial ? `<a href="${escapeHtml(comp.aerial)}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">View Aerial</a> &middot; ` : ''}<button class="pin-edit-btn" type="button" data-pin-key="${escapeHtml(comp._compKey||'')}" onclick="event.stopPropagation()">Change Pin</button> &middot; <button class="edit-btn" type="button" onclick="event.stopPropagation(); openEditDealModal('${escapeHtml(comp._compKey||'')}')">Edit Fields</button></div>`;
  return `<div class="comp-card" id="card-${idx}" onclick="zoomToComp(${idx})">
    <div class="comp-addr">${escapeHtml(comp.address||'(no address)')}</div>
    <div class="comp-meta">${type}${date}${units}${elc}${aerial}</div>
    <div class="comp-price-row"><span class="comp-price">${fmtPrice(comp.price)}</span><span class="comp-sub">${fmtAcres(comp.acreage)}${ppa?' &middot; '+ppa:''}${hasValue(comp.price_per_unit)?' &middot; $'+Math.round(Number(comp.price_per_unit)).toLocaleString()+'/unit':''}</span></div>
    ${parties}${links}
  </div>`;
}
function allFields(){return [...new Set(allComps.flatMap(c=>Object.keys(c)))].sort();}
function populateSelect(id,values){ const sel=document.getElementById(id); [...new Set(values.filter(hasValue))].sort((a,b)=>String(a).localeCompare(String(b))).forEach(v=>{ const opt=document.createElement('option'); opt.value=v; opt.textContent=v; sel.appendChild(opt); }); }
function populateFilters(){ allFields().forEach(f=>{ const opt=document.createElement('option'); opt.value=f; opt.textContent=FIELD_LABELS[f]||f; document.getElementById('field-filter').appendChild(opt); }); populateSelect('filter-type',allComps.map(c=>c.property_type)); populateSelect('filter-source',allComps.map(c=>c.source)); toggleUnitPriceFilters(); }
function passesSearch(c,q){ if(!q) return true; return Object.values(c).filter(hasValue).join(' ').toLowerCase().includes(q); }
function passesFieldFilter(c,field,val){ if(!field||!val) return true; if(!hasValue(c[field])) return false; return String(c[field]).toLowerCase().includes(val); }
function getFilteredMappedComps(){
  const q=document.getElementById('map-search').value.trim().toLowerCase();
  const field=document.getElementById('field-filter').value, fieldVal=document.getElementById('field-value').value.trim().toLowerCase();
  const type=document.getElementById('filter-type').value, source=document.getElementById('filter-source').value;
  const elc=document.getElementById('filter-elc').value, aerial=document.getElementById('filter-aerial').value;
  const dateFromVal=document.getElementById('sale-date-from').value, dateToVal=document.getElementById('sale-date-to').value;
  const dateFrom=dateFromVal?new Date(dateFromVal+'T00:00:00'):null, dateTo=dateToVal?new Date(dateToVal+'T23:59:59'):null;
  const minPrice=parseFloat(document.getElementById('min-price').value), maxPrice=parseFloat(document.getElementById('max-price').value);
  const minAcres=parseFloat(document.getElementById('min-acres').value), maxAcres=parseFloat(document.getElementById('max-acres').value);
  const minPPA=parseFloat(document.getElementById('min-ppa').value), maxPPA=parseFloat(document.getElementById('max-ppa').value);
  const minUnits=parseFloat(document.getElementById('min-units').value), maxUnits=parseFloat(document.getElementById('max-units').value);
  const minPPU=parseFloat(document.getElementById('min-ppu').value), maxPPU=parseFloat(document.getElementById('max-ppu').value);
  return allMappedComps.filter(c=>{
    if(type&&c.property_type!==type) return false;
    if(source&&c.source!==source) return false;
    if(elc==='yes'&&c.elc_deal!=='Yes') return false;
    if(elc==='no'&&c.elc_deal==='Yes') return false;
    if(aerial==='yes'&&!hasValue(c.aerial)) return false;
    if(aerial==='no'&&hasValue(c.aerial)) return false;
    const compDate=parseCompDate(c);
    if(dateFrom&&(!compDate||compDate<dateFrom)) return false;
    if(dateTo&&(!compDate||compDate>dateTo)) return false;
    if(!Number.isNaN(minPrice)&&(!hasValue(c.price)||Number(c.price)<minPrice)) return false;
    if(!Number.isNaN(maxPrice)&&(!hasValue(c.price)||Number(c.price)>maxPrice)) return false;
    if(!Number.isNaN(minAcres)&&(!hasValue(c.acreage)||Number(c.acreage)<minAcres)) return false;
    if(!Number.isNaN(maxAcres)&&(!hasValue(c.acreage)||Number(c.acreage)>maxAcres)) return false;
    if(!Number.isNaN(minPPA)&&(!hasValue(c.price_per_acre)||Number(c.price_per_acre)<minPPA)) return false;
    if(!Number.isNaN(maxPPA)&&(!hasValue(c.price_per_acre)||Number(c.price_per_acre)>maxPPA)) return false;
    if(!Number.isNaN(minUnits)&&(!hasValue(c.unit_count)||Number(c.unit_count)<minUnits)) return false;
    if(!Number.isNaN(maxUnits)&&(!hasValue(c.unit_count)||Number(c.unit_count)>maxUnits)) return false;
    if(!Number.isNaN(minPPU)&&(!hasValue(c.price_per_unit)||Number(c.price_per_unit)<minPPU)) return false;
    if(!Number.isNaN(maxPPU)&&(!hasValue(c.price_per_unit)||Number(c.price_per_unit)>maxPPU)) return false;
    return passesSearch(c,q)&&passesFieldFilter(c,field,fieldVal);
  });
}
function toggleUnitPriceFilters(){ const show=document.getElementById('filter-type')?.value==='Multifamily'; document.querySelectorAll('.ppu-filter,.unit-filter').forEach(el=>{el.style.display=show?'':'none';}); if(!show){['min-units','max-units','min-ppu','max-ppu'].forEach(id=>{const el=document.getElementById(id);if(el)el.value='';});} }
function applyFilters(){
  if(!graphicsLayer) return;
  filteredMappedComps=getFilteredMappedComps();
  graphicsLayer.removeAll(); activeGraphics=[];
  document.getElementById('map-count').textContent=`${filteredMappedComps.length.toLocaleString()} mapped comps shown | ${allMappedComps.length.toLocaleString()} mapped total | ${allComps.length.toLocaleString()} total comps`;
  document.getElementById('comp-list').innerHTML=filteredMappedComps.length?filteredMappedComps.map((c,i)=>buildListCard(c,i)).join(''):'<div style="padding:14px;font-size:12px;color:#667085">No mapped comps match the current filters.</div>';
  filteredMappedComps.forEach((comp,idx)=>{
    const graphic=new Graphic({geometry:{type:'point',longitude:comp.lon,latitude:comp.lat},symbol:{type:'simple-marker',color:getPriceColor(comp.price),size:getAcreSize(comp.acreage),outline:{color:[255,255,255,210],width:1}},attributes:{idx,compKey:comp._compKey},popupTemplate:{title:comp.address||'(no address)',content:function(){return buildPopupContent(comp);}}});
    activeGraphics.push(graphic); graphicsLayer.add(graphic);
  });
}
function addFilterListeners(){ ['map-search','field-filter','field-value','sale-date-from','sale-date-to','filter-type','filter-source','filter-elc','filter-aerial','min-price','max-price','min-acres','max-acres','min-ppa','max-ppa','min-units','max-units','min-ppu','max-ppu'].forEach(id=>{ document.getElementById(id).addEventListener('input',()=>{if(id==='filter-type') toggleUnitPriceFilters(); applyFilters();}); }); }
window.startPinEdit=function(compKey){
  const comp=getCompByKey(compKey);
  if(!comp||!hasValue(comp.lat)||!hasValue(comp.lon)){alert('This comp does not have a mapped location to edit.');return;}
  pinEditComp=comp; document.body.classList.add('pin-editing');
  const title=document.getElementById('pin-edit-title'), copy=document.getElementById('pin-edit-copy'), banner=document.getElementById('pin-edit-banner');
  if(title) title.textContent='Change pin location';
  if(copy) copy.textContent='Click the correct location on the map for: '+(comp.address||'selected comp')+'. You will be asked to confirm before it saves.';
  if(banner) banner.style.display='block';
  if(view){ view.goTo({center:[Number(comp.lon),Number(comp.lat)],zoom:16}).catch(()=>{}); }
};
window.cancelPinEdit=function(){ pinEditComp=null; document.body.classList.remove('pin-editing'); const banner=document.getElementById('pin-edit-banner'); if(banner) banner.style.display='none'; };
document.addEventListener('click', function(event){ const btn=event.target.closest&&event.target.closest('.pin-edit-link, .pin-edit-btn'); if(!btn) return; const key=btn.getAttribute('data-pin-key'); if(!key) return; event.preventDefault(); event.stopPropagation(); if(view&&view.popup){view.popup.close();} window.startPinEdit(key); }, true);
function savePinEdit(comp,lon,lat){ const overrides=loadPinOverrides(); overrides[comp._compKey]={lon:Number(lon),lat:Number(lat),updated:new Date().toISOString(),address:comp.address||null}; savePinOverrides(overrides); allComps=applyPinOverrides(allComps); allMappedComps=allComps.filter(c=>hasValue(c.lat)&&hasValue(c.lon)).map((c,i)=>({...c,_mappedIndex:i})); const key=comp._compKey; cancelPinEdit(); applyFilters(); const newIdx=filteredMappedComps.findIndex(c=>c._compKey===key); if(newIdx>=0) setTimeout(()=>zoomToComp(newIdx),100); }
window.resetFilters=function(){ ['map-search','field-value','sale-date-from','sale-date-to','min-price','max-price','min-acres','max-acres','min-ppa','max-ppa','min-units','max-units','min-ppu','max-ppu'].forEach(id=>document.getElementById(id).value=''); ['field-filter','filter-type','filter-source','filter-elc','filter-aerial'].forEach(id=>document.getElementById(id).value=''); toggleUnitPriceFilters(); applyFilters(); };
window.zoomToComp=function(idx){ const comp=filteredMappedComps[idx]; if(!comp) return; view.goTo({center:[comp.lon,comp.lat],zoom:16}); document.querySelectorAll('.comp-card').forEach(c=>c.classList.remove('active')); const card=document.getElementById('card-'+idx); if(card){card.classList.add('active');card.scrollIntoView({behavior:'smooth',block:'nearest'});} if(activeGraphics[idx]){if(view.openPopup){view.openPopup({features:[activeGraphics[idx]],location:activeGraphics[idx].geometry});}else if(view.popup&&view.popup.open){view.popup.open({features:[activeGraphics[idx]],location:activeGraphics[idx].geometry});}} };
window.zoomToResults=function(){ if(!view||!filteredMappedComps.length) return; if(filteredMappedComps.length===1){const c=filteredMappedComps[0];view.goTo({center:[c.lon,c.lat],zoom:15});return;} const lons=filteredMappedComps.map(c=>Number(c.lon)),lats=filteredMappedComps.map(c=>Number(c.lat)); view.goTo({target:{type:'extent',xmin:Math.min(...lons),ymin:Math.min(...lats),xmax:Math.max(...lons),ymax:Math.max(...lats),spatialReference:{wkid:4326}},padding:window.innerWidth<=760?{left:35,right:35,top:45,bottom:document.body.classList.contains('panel-collapsed')?70:Math.round(window.innerHeight*0.48)}:{left:400,right:50,top:50,bottom:50}}); };
window.toggleMobileFilters=function(){ document.body.classList.toggle('filters-collapsed'); const btn=document.getElementById('mobile-filter-toggle'); if(btn) btn.textContent=document.body.classList.contains('filters-collapsed')?'Show Filters':'Hide Filters'; };
window.toggleMobilePanel=function(){ document.body.classList.toggle('panel-collapsed'); const btn=document.getElementById('mobile-panel-toggle'); if(btn){const collapsed=document.body.classList.contains('panel-collapsed');btn.textContent=collapsed?'⌃':'⌄';btn.setAttribute('aria-label',collapsed?'Show comp list':'Minimize comp list');} if(view) view.padding=document.body.classList.contains('panel-collapsed')?{bottom:60}:{bottom:Math.round(window.innerHeight*0.46)}; };
if(window.innerWidth<=760){document.body.classList.add('filters-collapsed');setTimeout(()=>{const b=document.getElementById('mobile-filter-toggle');if(b)b.textContent='Show Filters';},0);}
require(["esri/Map","esri/views/MapView","esri/Graphic","esri/layers/GraphicsLayer"],function(Map,MapView,EsriGraphic,GraphicsLayer){
  Graphic=EsriGraphic;
  async function init(){
    const data=await(await fetch('./comps.json')).json();
    { const aerialBackup=await loadAerialBackup(); const baseComps=mergeAerialsFromBackup(data.comps||[], aerialBackup?.comps || aerialBackup || []); allComps=applyPinOverrides(applyLocalEdits([...baseComps,...loadLocalDeals()])); }
    allMappedComps=allComps.filter(c=>hasValue(c.lat)&&hasValue(c.lon)).map((c,i)=>({...c,_mappedIndex:i}));
    graphicsLayer=new GraphicsLayer();
    const map=new Map({basemap:"hybrid",layers:[graphicsLayer]});
    view=new MapView({container:"viewDiv",map,center:[-82.2,28.1],zoom:8});
    populateFilters(); addFilterListeners(); applyFilters();
    view.on('click',function(event){
      if(!pinEditComp) return; event.stopPropagation();
      const pt=event.mapPoint; if(!pt) return;
      const lon=Number(pt.longitude), lat=Number(pt.latitude);
      const ok=confirm('Move this comp pin to the location you just clicked?\n\n'+(pinEditComp.address||'Selected comp')+'\nLatitude: '+lat.toFixed(6)+'\nLongitude: '+lon.toFixed(6));
      if(ok) savePinEdit(pinEditComp,lon,lat);
    });
    view.when(()=>zoomToResults());
  }
  init();
});
