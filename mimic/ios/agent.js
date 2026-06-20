'use strict';
/* MimicAgent — injected into SpringBoard. System-wide control via IOHIDEvent (no per-app injection → defeats anti-frida). */
function sym(m, n){ return Module.findExportByName(m, n) || Module.findExportByName(null, n); }

var machTime = new NativeFunction(sym(null,'mach_absolute_time'), 'uint64', []);
var IOHIDEventSystemClientCreate = new NativeFunction(sym('IOKit','IOHIDEventSystemClientCreate'), 'pointer', ['pointer']);
var IOHIDEventSystemClientDispatchEvent = new NativeFunction(sym('IOKit','IOHIDEventSystemClientDispatchEvent'), 'void', ['pointer','pointer']);
var IOHIDEventCreateDigitizerEvent = new NativeFunction(sym('IOKit','IOHIDEventCreateDigitizerEvent'), 'pointer',
  ['pointer','uint64','uint32','uint32','uint32','uint32','uint32','double','double','double','double','double','uint32','uint32','uint32']);
var IOHIDEventCreateDigitizerFingerEvent = new NativeFunction(sym('IOKit','IOHIDEventCreateDigitizerFingerEvent'), 'pointer',
  ['pointer','uint64','uint32','uint32','uint32','double','double','double','double','double','uint32','uint32','uint32']);
var IOHIDEventAppendEvent = new NativeFunction(sym('IOKit','IOHIDEventAppendEvent'), 'void', ['pointer','pointer','uint32']);
var IOHIDEventSetIntegerValue = new NativeFunction(sym('IOKit','IOHIDEventSetIntegerValue'), 'void', ['pointer','uint32','int']);
var IOHIDEventSetSenderID = new NativeFunction(sym('IOKit','IOHIDEventSetSenderID'), 'void', ['pointer','uint64']);
var CFRelease = new NativeFunction(sym(null,'CFRelease'), 'void', ['pointer']);
var IOHIDEventCreateKeyboardEvent = new NativeFunction(sym('IOKit','IOHIDEventCreateKeyboardEvent'), 'pointer',
  ['pointer','uint64','uint32','uint32','int','uint32']);

var kRange=0x01, kTouch=0x02, kPosition=0x04;
var kHand=3;            // kIOHIDDigitizerTransducerTypeHand
var kFieldDisplayIntegrated = 0xb000a;
var SENDER = uint64('0x8000000817319375');
var W=750, H=1334;
try { var nb=ObjC.classes.UIScreen.mainScreen().nativeBounds(); if(nb[1][0]>100){W=Math.round(nb[1][0]);H=Math.round(nb[1][1]);} } catch(e){}

var client = IOHIDEventSystemClientCreate(NULL);

function digitizer(nx, ny, down){
  var mask = kPosition | kTouch | kRange;
  var touch = down?1:0, range = down?1:0;
  var t = machTime();
  var parent = IOHIDEventCreateDigitizerEvent(NULL, t, kHand, 0, 0, mask, 0, nx, ny, 0, 0, 0, range, touch, 0);
  IOHIDEventSetIntegerValue(parent, kFieldDisplayIntegrated, 1);
  IOHIDEventSetSenderID(parent, SENDER);
  var finger = IOHIDEventCreateDigitizerFingerEvent(NULL, t, 1, 2, mask, nx, ny, 0, 0, 0, range, touch, 0);
  IOHIDEventSetIntegerValue(finger, kFieldDisplayIntegrated, 1);
  IOHIDEventAppendEvent(parent, finger, 0);
  IOHIDEventSystemClientDispatchEvent(client, parent);
  CFRelease(finger); CFRelease(parent);
}
function sleepMs(ms){ var t=Date.now(); while(Date.now()-t<ms){} }

function swipe(x1,y1,x2,y2,ms){
  var steps=Math.max(8, Math.round((ms||300)/16));
  digitizer(x1/W,y1/H,true);
  for(var i=1;i<=steps;i++){ var f=i/steps; digitizer((x1+(x2-x1)*f)/W,(y1+(y2-y1)*f)/H,true); sleepMs(Math.round((ms||300)/steps)); }
  digitizer(x2/W,y2/H,false);
}
function consumerKey(usage){
  // usage page 0x0C consumer; menu/home = 0x40
  var d = IOHIDEventCreateKeyboardEvent(NULL, machTime(), 0x0C, usage, 1, 0);
  IOHIDEventSystemClientDispatchEvent(client, d); CFRelease(d);
  var u = IOHIDEventCreateKeyboardEvent(NULL, machTime(), 0x0C, usage, 0, 0);
  IOHIDEventSystemClientDispatchEvent(client, u); CFRelease(u);
}
function shot(){
  var s=sym('UIKitCore','_UICreateScreenUIImage'), p=sym('UIKitCore','UIImagePNGRepresentation');
  var mk=new NativeFunction(s,'pointer',[]), png=new NativeFunction(p,'pointer',['pointer']);
  var img=mk(); var d=png(img); var nd=new ObjC.Object(d);
  var len=nd.length().valueOf();
  var b64sym=sym(null,'objc_msgSend'); // use ObjC base64
  var b64 = nd.base64EncodedStringWithOptions_(0).toString();
  return {w:W,h:H,png_b64:b64};
}

function appList(){
  var out=[];
  try{
    var ws=ObjC.classes.LSApplicationWorkspace.defaultWorkspace();
    var arr=ws.allInstalledApplications();
    for(var i=0;i<arr.count();i++){
      var p=arr.objectAtIndex_(i);
      try{
        var b=p.bundleIdentifier(); var n=p.localizedName();
        out.push({bundle:b?b.toString():'', name:n?n.toString():''});
      }catch(e){}
    }
  }catch(e){ return {err:''+e}; }
  return out;
}
var IOPMAssertionCreateWithName = new NativeFunction(sym('IOKit','IOPMAssertionCreateWithName'), 'int', ['pointer','uint32','pointer','pointer']);
var _assertion = Memory.alloc(4);
function keepAwake(){
  try{
    var type=ObjC.classes.NSString.stringWithString_('PreventUserIdleDisplaySleep');
    var name=ObjC.classes.NSString.stringWithString_('Mimic');
    return IOPMAssertionCreateWithName(type.handle, 255, name.handle, _assertion); // 0 = kIOReturnSuccess
  }catch(e){ return -99; }
}
function wakeScreen(){
  try{ ObjC.classes.SBBacklightController.sharedInstance().turnOnScreenFullyWithBacklightSource_(0); return 1; }
  catch(e){ try{ var f=sym('SpringBoardServices','SBSUndimScreen'); if(f){ new NativeFunction(f,'void',[])(); return 1; } }catch(e2){} return {err:''+e}; }
}
function unlockScreen(){
  try{ ObjC.classes.SBLockScreenManager.sharedInstance().unlockUIFromSource_withOptions_(0, NULL); return 1; }
  catch(e){ return {err:''+e}; }
}
// --- SSLKillSwitch3 control (NyaMisty) -------------------------------------
// The tweak reads `shouldDisableCertificateValidation` straight from its prefs
// FILE at each hooked process's init (no Darwin notification), so we read/write
// that file directly — the same way the tweak does — rather than via CFPreferences
// (whose cfprefsd write-back could lag the file the tweak actually loads). `paths`
// is a candidate list (jb-prefixed first); we use the one that exists.
var SSL_KEY = "shouldDisableCertificateValidation";
function sslRead(paths){
  var fm = ObjC.classes.NSFileManager.defaultManager();
  for(var i=0;i<paths.length;i++){
    if(fm.fileExistsAtPath_(paths[i])){
      var d = ObjC.classes.NSDictionary.dictionaryWithContentsOfFile_(paths[i]);
      var v = d ? d.objectForKey_(SSL_KEY) : null;
      return { ok:1, found:1, path:''+paths[i], bypass: (v && v.boolValue()) ? 1 : 0 };
    }
  }
  return { ok:1, found:0, path: paths.length ? (''+paths[0]) : null, bypass:0 };
}
function sslWrite(paths, on){
  var fm = ObjC.classes.NSFileManager.defaultManager();
  var target = null;
  for(var i=0;i<paths.length;i++){ if(fm.fileExistsAtPath_(paths[i])){ target=paths[i]; break; } }
  if(!target) target = paths[0];
  var d = ObjC.classes.NSMutableDictionary.dictionaryWithContentsOfFile_(target);
  if(!d) d = ObjC.classes.NSMutableDictionary.dictionary();
  d.setObject_forKey_(ObjC.classes.NSNumber.numberWithBool_(on?1:0), SSL_KEY);
  var ok = d.writeToFile_atomically_(target, 1) ? 1 : 0;
  return { ok:ok, path:''+target, bypass: on?1:0 };
}
function launchApp(bundle){
  try{
    var ws=ObjC.classes.LSApplicationWorkspace.defaultWorkspace();
    var ok=ws.openApplicationWithBundleID_(bundle);
    return ok ? 1 : 0;
  }catch(e){ return {err:''+e}; }
}

// Walk the foreground app's UIView hierarchy and return actionable/labeled
// elements with screen-POINT frames (cx,cy = tap center in points). Run when
// agent.js is injected into the FOREGROUND app (not SpringBoard).
function dumpUI(){
  var out = [];
  try {
    var app = ObjC.classes.UIApplication.sharedApplication();
    var wins = app.windows();
    var nilView = new NativePointer(0);
    function role(v){
      try {
        if (v.isKindOfClass_(ObjC.classes.UITextField) || v.isKindOfClass_(ObjC.classes.UITextView) || v.isKindOfClass_(ObjC.classes.UISearchBar)) return 'field';
        if (v.isKindOfClass_(ObjC.classes.UISwitch)) return 'switch';
        if (v.isKindOfClass_(ObjC.classes.UISlider)) return 'slider';
        if (v.isKindOfClass_(ObjC.classes.UIButton)) return 'btn';
        if (v.isKindOfClass_(ObjC.classes.UIControl)) return 'ctl';
        if (v.isKindOfClass_(ObjC.classes.UILabel)) return 'txt';
      } catch (e) {}
      return null;
    }
    function text(v){
      try { if (v.accessibilityLabel && !v.accessibilityLabel().isNull && v.accessibilityLabel()) return v.accessibilityLabel().toString(); } catch (e) {}
      try { if (v.respondsToSelector_(ObjC.selector('text')) && v.text()) return v.text().toString(); } catch (e) {}
      try { if (v.respondsToSelector_(ObjC.selector('title')) && v.title()) return v.title().toString(); } catch (e) {}
      return '';
    }
    function walk(v, depth){
      if (depth > 40) return;
      try {
        if (v.isHidden && v.isHidden()) return;
        if (v.alpha && v.alpha() < 0.05) return;
        var r = role(v);
        var lbl = '';
        try { if (v.accessibilityLabel && v.accessibilityLabel()) lbl = v.accessibilityLabel().toString(); } catch (e) {}
        if (!lbl) lbl = text(v);
        if (r || lbl) {
          var rect = v.convertRect_toView_(v.bounds(), nilView); // -> window points (~screen for fullscreen window)
          var x = rect[0][0], y = rect[0][1], w = rect[1][0], h = rect[1][1];
          if (w > 2 && h > 2 && w < 2000 && h < 2000) {
            out.push({ role: r || 'txt', label: lbl, x: Math.round(x), y: Math.round(y),
                       w: Math.round(w), h: Math.round(h), cx: Math.round(x + w/2), cy: Math.round(y + h/2) });
          }
        }
        var subs = v.subviews();
        for (var i = 0; i < subs.count(); i++) walk(subs.objectAtIndex_(i), depth + 1);
      } catch (e) {}
    }
    for (var k = 0; k < wins.count(); k++) walk(wins.objectAtIndex_(k), 0);
  } catch (e) { return { err: '' + e }; }
  return out;
}

// Activate an element by its accessibility label via accessibilityActivate()
// (VoiceOver-style — performs the element's action without an HID touch).
function axActivate(label, idx){
  idx = idx || 0;
  var want = ('' + label).toLowerCase();
  var matches = [];
  function txts(v){
    var out = [];
    try { if (v.accessibilityLabel && v.accessibilityLabel()) out.push(v.accessibilityLabel().toString()); } catch (e) {}
    try { if (v.respondsToSelector_(ObjC.selector('currentTitle')) && v.currentTitle()) out.push(v.currentTitle().toString()); } catch (e) {}
    try { if (v.respondsToSelector_(ObjC.selector('title')) && v.title()) out.push(v.title().toString()); } catch (e) {}
    try { if (v.respondsToSelector_(ObjC.selector('text')) && v.text()) out.push(v.text().toString()); } catch (e) {}
    return out;
  }
  function walk(v, depth){
    if (depth > 45) return;
    try {
      if (v.isHidden && v.isHidden()) return;
      var ts = txts(v);
      for (var t = 0; t < ts.length; t++){ if (ts[t].toLowerCase() === want) { matches.push(v); break; } }
      var subs = v.subviews();
      for (var i = 0; i < subs.count(); i++) walk(subs.objectAtIndex_(i), depth + 1);
    } catch (e) {}
  }
  var wins = ObjC.classes.UIApplication.sharedApplication().windows();
  for (var k = 0; k < wins.count(); k++) walk(wins.objectAtIndex_(k), 0);
  if (!matches.length) return { err: 'no element labeled: ' + label };
  // For each match, find the nearest UIControl (itself or an ancestor) — the label
  // is often on a child of the real tappable control.
  function ctlFor(v){
    var cur = v, hops = 0;
    while (cur && hops < 8){
      try { if (cur.isKindOfClass_(ObjC.classes.UIControl)) return cur; } catch (e) {}
      try { cur = cur.superview(); } catch (e) { cur = null; }
      hops++;
    }
    return null;
  }
  // nearest ancestor (or self) that is kind of clsName
  function ancestorOfClass(v, clsName){
    var cls = ObjC.classes[clsName]; if (!cls) return null;
    var cur = v, hops = 0;
    while (cur && hops < 14){
      try { if (cur.isKindOfClass_(cls)) return cur; } catch (e) {}
      try { cur = cur.superview(); } catch (e) { cur = null; }
      hops++;
    }
    return null;
  }
  var v = matches[Math.min(idx, matches.length - 1)];
  // Pick the best actionable container across all matches, in priority order:
  // UIControl (fire action) > table/collection cell (invoke delegate didSelect).
  var ctl = null, cell = null, colCell = null;
  for (var m = 0; m < matches.length; m++){ var c = ctlFor(matches[m]); if (c){ ctl = c; v = matches[m]; break; } }
  if (!ctl){
    for (var m2 = 0; m2 < matches.length; m2++){
      var tc = ancestorOfClass(matches[m2], 'UITableViewCell');
      if (tc){ cell = tc; v = matches[m2]; break; }
      var cc = ancestorOfClass(matches[m2], 'UICollectionViewCell');
      if (cc){ colCell = cc; v = matches[m2]; break; }
    }
  }
  try {
    if (ctl){
      ctl.sendActionsForControlEvents_(1 << 6); // UIControlEventTouchUpInside = 64
      return { ok: 1, matched: matches.length, via: 'sendActions', cls: ctl.$className };
    }
    if (cell){
      var tv = ancestorOfClass(cell, 'UITableView');
      if (tv){
        var ip = tv.indexPathForCell_(cell);
        if (ip && !ip.isNull()){
          try { tv.selectRowAtIndexPath_animated_scrollPosition_(ip, false, 0); } catch (e) {}
          var dg = tv.delegate();
          if (dg && dg.respondsToSelector_(ObjC.selector('tableView:didSelectRowAtIndexPath:'))){
            dg.tableView_didSelectRowAtIndexPath_(tv, ip);
            return { ok: 1, matched: matches.length, via: 'didSelectRow', cls: cell.$className };
          }
        }
      }
    }
    if (colCell){
      var clv = ancestorOfClass(colCell, 'UICollectionView');
      if (clv){
        var ip2 = clv.indexPathForCell_(colCell);
        if (ip2 && !ip2.isNull()){
          var dg2 = clv.delegate();
          if (dg2 && dg2.respondsToSelector_(ObjC.selector('collectionView:didSelectItemAtIndexPath:'))){
            dg2.collectionView_didSelectItemAtIndexPath_(clv, ip2);
            return { ok: 1, matched: matches.length, via: 'didSelectItem', cls: colCell.$className };
          }
        }
      }
    }
    // Last resort: VoiceOver-style activate, then any tap gesture recognizer.
    var done = false;
    try { done = v.accessibilityActivate(); } catch (e) {}
    if (!done){
      try {
        var grs = v.gestureRecognizers();
        if (grs && grs.count()){
          for (var g = 0; g < grs.count(); g++){
            var gr = grs.objectAtIndex_(g);
            if (gr.isKindOfClass_(ObjC.classes.UITapGestureRecognizer)){
              try { gr.setState_(3); } catch (e) {} // UIGestureRecognizerStateEnded
            }
          }
        }
      } catch (e) {}
    }
    return { ok: 1, matched: matches.length, via: 'axActivate', cls: v.$className };
  } catch (e) { return { err: '' + e }; }
}

// Set text into a UITextField/UITextView found by label/placeholder, firing the
// editingChanged control event so the app reacts as if typed.
function axSetText(label, text){
  var want = ('' + label).toLowerCase();
  var found = null;
  function isField(v){ try { return v.isKindOfClass_(ObjC.classes.UITextField) || v.isKindOfClass_(ObjC.classes.UITextView) || v.isKindOfClass_(ObjC.classes.UISearchBar); } catch (e) { return false; } }
  function fieldLbl(v){
    var out = [];
    try { if (v.accessibilityLabel && v.accessibilityLabel()) out.push(v.accessibilityLabel().toString()); } catch (e) {}
    try { if (v.respondsToSelector_(ObjC.selector('placeholder')) && v.placeholder()) out.push(v.placeholder().toString()); } catch (e) {}
    return out.map(function(s){ return s.toLowerCase(); });
  }
  function walk(v, depth){
    if (found || depth > 45) return;
    try {
      if (isField(v)){
        if (!label || fieldLbl(v).indexOf(want) >= 0) { found = v; return; }
      }
      var subs = v.subviews();
      for (var i = 0; i < subs.count(); i++) { walk(subs.objectAtIndex_(i), depth + 1); if (found) return; }
    } catch (e) {}
  }
  var wins = ObjC.classes.UIApplication.sharedApplication().windows();
  for (var k = 0; k < wins.count(); k++) { walk(wins.objectAtIndex_(k), 0); if (found) break; }
  if (!found) return { err: 'no text field for: ' + label };
  try {
    var ns = ObjC.classes.NSString.stringWithString_(text);
    if (found.respondsToSelector_(ObjC.selector('becomeFirstResponder'))) found.becomeFirstResponder();
    if (found.isKindOfClass_(ObjC.classes.UISearchBar)) found.setText_(ns);
    else found.setText_(ns);
    // fire editingChanged for UIControl text fields
    try { if (found.isKindOfClass_(ObjC.classes.UIControl)) found.sendActionsForControlEvents_(1 << 16); } catch (e) {}
    // notify delegate / text-did-change
    try { ObjC.classes.NSNotificationCenter.defaultCenter().postNotificationName_object_('UITextFieldTextDidChangeNotification', found); } catch (e) {}
    return { ok: 1, cls: found.$className };
  } catch (e) { return { err: '' + e }; }
}

// ---- screen video recorder: CARenderServer -> IOSurface -> JPEG frames ----
// Captures the real composited display (foreground app included) from SpringBoard.
var _rs = {};
function rsInit(){
  if (_rs.ready) return;
  _rs.IOSurfaceCreate = new NativeFunction(sym('IOSurface','IOSurfaceCreate'),'pointer',['pointer']);
  _rs.IOSurfaceLock = new NativeFunction(sym('IOSurface','IOSurfaceLock'),'int',['pointer','uint32','pointer']);
  _rs.IOSurfaceUnlock = new NativeFunction(sym('IOSurface','IOSurfaceUnlock'),'int',['pointer','uint32','pointer']);
  _rs.IOSurfaceGetBaseAddress = new NativeFunction(sym('IOSurface','IOSurfaceGetBaseAddress'),'pointer',['pointer']);
  _rs.IOSurfaceGetBytesPerRow = new NativeFunction(sym('IOSurface','IOSurfaceGetBytesPerRow'),'pointer',['pointer']);
  _rs.CARender = new NativeFunction(sym('QuartzCore','CARenderServerRenderDisplay'),'void',['uint32','pointer','pointer','int32','int32']);
  _rs.CGColorSpaceCreateDeviceRGB = new NativeFunction(sym('CoreGraphics','CGColorSpaceCreateDeviceRGB'),'pointer',[]);
  _rs.CGBitmapContextCreate = new NativeFunction(sym('CoreGraphics','CGBitmapContextCreate'),'pointer',['pointer','uint32','uint32','uint32','uint32','pointer','uint32']);
  _rs.CGBitmapContextCreateImage = new NativeFunction(sym('CoreGraphics','CGBitmapContextCreateImage'),'pointer',['pointer']);
  _rs.CGImageRelease = new NativeFunction(sym('CoreGraphics','CGImageRelease'),'void',['pointer']);
  _rs.CGContextRelease = new NativeFunction(sym('CoreGraphics','CGContextRelease'),'void',['pointer']);
  _rs.CGColorSpaceRelease = new NativeFunction(sym('CoreGraphics','CGColorSpaceRelease'),'void',['pointer']);
  _rs.UIImageJPEG = new NativeFunction(sym('UIKitCore','UIImageJPEGRepresentation'),'pointer',['pointer','double']);
  _rs.lcd = ObjC.classes.NSString.stringWithString_('LCD');
  _rs.cs = _rs.CGColorSpaceCreateDeviceRGB();
  _rs.ready = true;
}
function rsCreateSurface(){
  var d = ObjC.classes.NSMutableDictionary.dictionary();
  var keys=['IOSurfaceWidth','IOSurfaceHeight','IOSurfaceBytesPerElement','IOSurfacePixelFormat'];
  var vals=[W,H,4,0x42475241]; // 'BGRA'
  for(var i=0;i<keys.length;i++) d.setObject_forKey_(ObjC.classes.NSNumber.numberWithLongLong_(vals[i]), ObjC.classes.NSString.stringWithString_(keys[i]));
  return _rs.IOSurfaceCreate(d.handle);
}
function rsFrameToFile(surf, path, q){
  _rs.CARender(0, _rs.lcd.handle, surf, 0, 0);
  _rs.IOSurfaceLock(surf, 1, NULL);
  var base = _rs.IOSurfaceGetBaseAddress(surf);
  var bpr = _rs.IOSurfaceGetBytesPerRow(surf).toInt32();
  var ctx = _rs.CGBitmapContextCreate(base, W, H, 8, bpr, _rs.cs, 0x2002); // PremultipliedFirst | ByteOrder32Little
  var cg = _rs.CGBitmapContextCreateImage(ctx);
  _rs.IOSurfaceUnlock(surf, 1, NULL);
  var ui = ObjC.classes.UIImage.imageWithCGImage_(cg);
  var jpg = new ObjC.Object(_rs.UIImageJPEG(ui, q));
  jpg.writeToFile_atomically_(ObjC.classes.NSString.stringWithString_(path), false);
  _rs.CGImageRelease(cg); _rs.CGContextRelease(ctx);
}
// One live frame -> base64 JPEG, captured BELOW the DRM secure layer (so it mirrors
// Netflix / banking apps that go-ios screenshot shows black). Reuses one surface for
// speed; runs on the frida thread (off main) like recRun.
var _liveSurf = null;
function rsFrameB64(q){
  rsInit();
  if (!_liveSurf || _liveSurf.isNull()) _liveSurf = rsCreateSurface();
  if (!_liveSurf || _liveSurf.isNull()) return null;
  var surf = _liveSurf;
  // EACH frame in its own autorelease pool — the UIImage / JPEG NSData / base64 NSString
  // are autoreleased, and without draining them per frame they pile up at 30-60 fps until
  // jetsam kills SpringBoard / reboots the device. (recRun does the same.)
  var pool = ObjC.classes.NSAutoreleasePool.alloc().init();
  var b64 = null;
  try {
    _rs.CARender(0, _rs.lcd.handle, surf, 0, 0);
    _rs.IOSurfaceLock(surf, 1, NULL);
    var base = _rs.IOSurfaceGetBaseAddress(surf);
    var bpr = _rs.IOSurfaceGetBytesPerRow(surf).toInt32();
    var ctx = _rs.CGBitmapContextCreate(base, W, H, 8, bpr, _rs.cs, 0x2002);
    var cg = _rs.CGBitmapContextCreateImage(ctx);
    _rs.IOSurfaceUnlock(surf, 1, NULL);
    var ui = ObjC.classes.UIImage.imageWithCGImage_(cg);
    var jpg = new ObjC.Object(_rs.UIImageJPEG(ui, q||0.4));
    b64 = jpg.base64EncodedStringWithOptions_(0).toString();
    _rs.CGImageRelease(cg); _rs.CGContextRelease(ctx);
  } finally {
    pool.release();
  }
  return b64;
}
function pad5(n){ n=''+n; while(n.length<5) n='0'+n; return n; }
// Record `secs` seconds at `fps` into dir as fNNNNN.jpg. Runs on the frida thread
// (not main) so the UI keeps updating and we capture live motion.
function recRun(dir, fps, secs, q){
  try{
    rsInit();
    ObjC.classes.NSFileManager.defaultManager().createDirectoryAtPath_withIntermediateDirectories_attributes_error_(ObjC.classes.NSString.stringWithString_(dir), true, NULL, NULL);
    var surf = rsCreateSurface();
    if (surf.isNull()) return { err: 'IOSurfaceCreate failed' };
    var total = Math.max(1, Math.round(fps*secs));
    var interval = 1000.0/fps;
    var t0 = Date.now(), wrote = 0;
    for (var i=0; i<total; i++){
      var pool = ObjC.classes.NSAutoreleasePool.alloc().init();
      try { rsFrameToFile(surf, dir + '/f' + pad5(i) + '.jpg', q||0.5); wrote++; } catch(e){}
      pool.release();
      var target = t0 + (i+1)*interval;
      var rem = target - Date.now();
      if (rem > 1) Thread.sleep(rem/1000.0);
    }
    CFRelease(surf);
    var dt = (Date.now()-t0)/1000.0;
    return { ok:1, frames:wrote, secs:dt, fps: wrote/dt, w:W, h:H };
  }catch(e){ return { err: ''+e }; }
}
function readFileB64(path){
  try{
    var dt = ObjC.classes.NSData.dataWithContentsOfFile_(ObjC.classes.NSString.stringWithString_(path));
    if (!dt || dt.isNull()) return null;
    return dt.base64EncodedStringWithOptions_(0).toString();
  }catch(e){ return null; }
}

// speak text straight into the live call's telephony UPLINK so the callee hears
// it — Apple's official AVSpeechSynthesizer.mixToTelephonyUplink (iOS 13+). No
// acoustic relay, no baseband fight: iOS mixes the synthesized speech into the
// call's outgoing audio. Run from the in-call process during an active call.
var _synth = null;
// pick the highest-quality installed voice for a language (premium > enhanced >
// default) so the callee hears the best available system voice.
function bestVoice(lang){
  try{
    var pref = (lang || 'th-TH').split('-')[0];
    var voices = ObjC.classes.AVSpeechSynthesisVoice.speechVoices();
    var best = null, bestQ = -1;
    for (var i = 0; i < voices.count(); i++){
      var v = voices.objectAtIndex_(i);
      if (('' + v.language()).indexOf(pref) === 0){
        var q = 1; try { q = v.quality(); } catch(e){}
        if (q > bestQ){ bestQ = q; best = v; }
      }
    }
    return best;
  }catch(e){ return null; }
}
function speakUplink(text, lang, rate, pitch){
  try{
    var s = ObjC.classes.AVSpeechSynthesizer.alloc().init();
    try{ s.setUsesApplicationAudioSession_(0); }catch(e){}
    try{ s.setMixToTelephonyUplink_(1); }catch(e){}
    var u = ObjC.classes.AVSpeechUtterance.speechUtteranceWithString_(
              ObjC.classes.NSString.stringWithString_(text));
    var v = bestVoice(lang) ||
            ObjC.classes.AVSpeechSynthesisVoice.voiceWithLanguage_(
              ObjC.classes.NSString.stringWithString_(lang || 'th-TH'));
    var vname = '?';
    if (v && !v.isNull()){ u.setVoice_(v); try { vname = '' + v.name(); } catch(e){} }
    u.setRate_(rate || 0.5);
    // pitchMultiplier (0.5-2.0): <1 deepens the voice. Used to fake a deeper/more
    // masculine tone when only a female voice is installed for the language.
    if (pitch && pitch > 0){ try{ u.setPitchMultiplier_(pitch); }catch(e){} }
    u.setVolume_(1.0);
    s.speakUtterance_(u);
    _synth = s; // retain across the async speech
    var mix = '?'; try { mix = s.mixToTelephonyUplink(); } catch(e){}
    return { ok: 1, mix: mix, voice: vname };
  }catch(e){ return { err: '' + e }; }
}

// ---- per-app SSL unpinning (run inside the target app) ----
// Hooks the trust checks the iOS TLS stack actually uses, so a proxy can MITM HTTPS even
// where SSLKillSwitch3 doesn't reach (BoringSSL custom-verify covers NSURLSession/CFNetwork;
// SecTrust covers the higher-level evaluations). Installed once; callbacks kept alive.
var _sslUnpinned = false, _okVerifyCb = null;
function sslUnpin(){
  if (_sslUnpinned) return { ok: 1, already: 1 };
  var hooked = [], errs = [];
  try {
    _okVerifyCb = new NativeCallback(function(){ return 0; /* ssl_verify_ok */ }, 'int', ['pointer','pointer']);
    ['SSL_set_custom_verify','SSL_CTX_set_custom_verify'].forEach(function(fn){
      var p = Module.findExportByName(null, fn);
      if (p) { Interceptor.attach(p, { onEnter: function(a){ a[2] = _okVerifyCb; } }); hooked.push(fn); }
    });
  } catch(e){ errs.push('boringssl: ' + e); }
  try {
    var stwe = Module.findExportByName(null, 'SecTrustEvaluateWithError');
    if (stwe) { Interceptor.attach(stwe, { onLeave: function(r){ r.replace(0x1); } }); hooked.push('SecTrustEvaluateWithError'); }
  } catch(e){ errs.push('stwe: ' + e); }
  try {
    var ste = Module.findExportByName(null, 'SecTrustEvaluate');
    if (ste) { Interceptor.attach(ste, {
      onEnter: function(a){ this.res = a[1]; },
      onLeave: function(r){ if (this.res && !this.res.isNull()) { try { this.res.writeU32(1); /* kSecTrustResultProceed */ } catch(e){} } r.replace(0); }
    }); hooked.push('SecTrustEvaluate'); }
  } catch(e){ errs.push('ste: ' + e); }
  _sslUnpinned = hooked.length > 0;
  return { ok: _sslUnpinned ? 1 : 0, hooked: hooked, errs: errs };
}

rpc.exports = {
  ui: function(){ var r=null; ObjC.schedule(ObjC.mainQueue,function(){ try{r=dumpUI();}catch(e){r={err:''+e};} }); var n=0; while(r===null&&n<400){Thread.sleep(0.01);n++;} return r; },
  activate: function(label, idx){ var r=null; ObjC.schedule(ObjC.mainQueue,function(){ try{r=axActivate(label, idx);}catch(e){r={err:''+e};} }); var n=0; while(r===null&&n<300){Thread.sleep(0.01);n++;} return r; },
  setText: function(label, text){ var r=null; ObjC.schedule(ObjC.mainQueue,function(){ try{r=axSetText(label, text);}catch(e){r={err:''+e};} }); var n=0; while(r===null&&n<300){Thread.sleep(0.01);n++;} return r; },
  apps: function(){ var r=null; ObjC.schedule(ObjC.mainQueue,function(){r=appList();}); var n=0; while(r===null&&n<300){Thread.sleep(0.01);n++;} return r; },
  launch: function(b){ return launchApp(b); },
  keepAwake: function(){ return keepAwake(); },
  wake: function(){ var r=null; ObjC.schedule(ObjC.mainQueue,function(){r=wakeScreen();}); var n=0; while(r===null&&n<200){Thread.sleep(0.01);n++;} return r; },
  unlock: function(){ var r=null; ObjC.schedule(ObjC.mainQueue,function(){r=unlockScreen();}); var n=0; while(r===null&&n<200){Thread.sleep(0.01);n++;} return r; },
  // Wake + actually land past the lock screen. On iOS 16.7 unlockUIFromSource: reports
  // success but doesn't always tear down the lock UI, so we follow it with a Home press
  // (what really dismisses to the home screen on a no-passcode device) and re-check
  // isUILocked until it clears. Conditional on lock state: if already unlocked (e.g. mid
  // app) it's a no-op, so it won't kick the user out to home. ObjC/HID run on the main
  // queue; the wait/retry loop sleeps on the frida thread so the lock UI can tear down.
  wakeUnlock: function(){
    function sched(fn){ var r=null; ObjC.schedule(ObjC.mainQueue,function(){ try{r=fn();}catch(e){r={err:''+e};} }); var n=0; while(r===null&&n<300){Thread.sleep(0.01);n++;} return r; }
    function locked(){ var v=sched(function(){ try{return ObjC.classes.SBLockScreenManager.sharedInstance().isUILocked()?1:0;}catch(e){return -1;} }); return (typeof v==='number')?v:-1; }
    sched(function(){ try{ObjC.classes.SBBacklightController.sharedInstance().turnOnScreenFullyWithBacklightSource_(0);}catch(e){} return 1; });
    var lk=locked();
    for(var i=0;i<5 && lk!==0;i++){
      sched(function(){ try{ObjC.classes.SBLockScreenManager.sharedInstance().unlockUIFromSource_withOptions_(0,NULL);}catch(e){} consumerKey(0x40); return 1; });
      Thread.sleep(0.45);
      lk=locked();
    }
    return { ok: lk===0?1:0, locked: lk };
  },
  swipe: function(x1,y1,x2,y2,ms){ swipe(x1,y1,x2,y2,ms); return true; },
  home: function(){ ObjC.schedule(ObjC.mainQueue,function(){consumerKey(0x40);}); return true; },
  // Hardware buttons via the same proven Consumer-HID path as home (usage page 0x0C):
  // volUp 0xE9, volDown 0xEA, mute 0xE2, power/lock 0x30, home 0x40. A quick down+up =
  // a short press (lock, not power-off). consumerKey is lightweight HID dispatch, so it
  // is safe (home has used it all along) — unlike digitizer taps it isn't a dead-end.
  hwkey: function(usage){ ObjC.schedule(ObjC.mainQueue,function(){consumerKey(usage);}); return true; },
  shot: function(){ var out=null; ObjC.schedule(ObjC.mainQueue,function(){ try{out=shot();}catch(e){out={err:''+e};} }); var n=0; while(out===null&&n<200){ Thread.sleep(0.01); n++; } return out; },
  // video: runs on the frida thread (off main) so the UI keeps animating while we capture
  recRun: function(dir, fps, secs, q){ return recRun(dir, fps, secs, q); },
  frame: function(q){ try { return rsFrameB64(q); } catch(e){ return null; } },
  // live finger: down/move/up via the digitizer (drives scroll/pan like a real touch).
  touchDown: function(x,y){ digitizer(x/W, y/H, true); return true; },
  touchMove: function(x,y){ digitizer(x/W, y/H, true); return true; },
  touchUp: function(x,y){ digitizer(x/W, y/H, false); return true; },
  readFile: function(path){ return readFileB64(path); },
  speakUplink: function(text, lang, rate, pitch){ return speakUplink(text, lang, rate, pitch); },
  uplinkSpeaking: function(){ try { return !!(_synth && _synth.isSpeaking()); } catch(e){ return false; } },
  sslGet: function(paths){ return sslRead(paths); },
  sslSet: function(paths, on){ return sslWrite(paths, on?1:0); },
  sslUnpin: function(){ return sslUnpin(); },
};
