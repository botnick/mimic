'use strict';
/* MimicTel — telephony + TTS agent, injected into SpringBoard.
 * Places a call, detects when the callee answers (CTCallCenter state), routes
 * audio to the loudspeaker and speaks synthesized text so the callee hears it
 * (near-end speech is preserved by the call's echo canceller and sent uplink).
 */
function sym(m, n){ return Module.findExportByName(m, n) || Module.findExportByName(null, n); }
var G = {};

var _CTCallDisconnect = sym('CoreTelephony', 'CTCallDisconnect');
var CTCallDisconnect = _CTCallDisconnect ? new NativeFunction(_CTCallDisconnect, 'int', ['pointer']) : null;

// AudioServices: fire-and-forget system-sound playback. Does NOT touch the
// AVAudioSession (so it can't crash SpringBoard like setActive did) — on a
// speakerphone call the sound goes out the loudspeaker and the baseband mic
// relays it to the callee (the baseband AEC references its own downlink, not
// this iOS sound, so it isn't cancelled).
var _ASCreate = sym('AudioToolbox', 'AudioServicesCreateSystemSoundID');
var _ASPlay   = sym('AudioToolbox', 'AudioServicesPlaySystemSound');
var ASCreate  = _ASCreate ? new NativeFunction(_ASCreate, 'int', ['pointer', 'pointer']) : null;
var ASPlay    = _ASPlay ? new NativeFunction(_ASPlay, 'void', ['uint32']) : null;

function writeFile(path, b64) {
  try {
    var data = ObjC.classes.NSData.alloc().initWithBase64EncodedString_options_(
      ObjC.classes.NSString.stringWithString_(b64), 0);
    var ok = data.writeToFile_atomically_(ObjC.classes.NSString.stringWithString_(path), 1);
    return { ok: ok ? 1 : 0, bytes: data.length().valueOf() };
  } catch (e) { return { err: '' + e }; }
}

function playSound(path) {
  try {
    if (!ASCreate || !ASPlay) return { err: 'no AudioServices' };
    var url = ObjC.classes.NSURL.fileURLWithPath_(ObjC.classes.NSString.stringWithString_(path));
    var sidp = Memory.alloc(4);
    var r = ASCreate(url.handle, sidp);
    var sid = sidp.readU32();
    ASPlay(sid);
    return { ok: r === 0 ? 1 : 0, ret: r, sid: sid };
  } catch (e) { return { err: '' + e }; }
}

function currentCalls(){
  var cc = ObjC.classes.CTCallCenter.alloc().init();
  var s = cc.currentCalls();
  if (!s || s.isNull()) return [];
  var a = s.allObjects(); var out = [];
  for (var i = 0; i < a.count(); i++) out.push(a.objectAtIndex_(i));
  return out;
}

function dial(num){
  try {
    var url = ObjC.classes.NSURL.URLWithString_(ObjC.classes.NSString.stringWithString_('tel://' + num));
    var ws = ObjC.classes.LSApplicationWorkspace.defaultWorkspace();
    var r = ws.openSensitiveURL_withOptions_(url, NULL);
    return { ok: 1, ret: '' + r };
  } catch (e) { return { err: '' + e }; }
}

// Coarse state: 'none' | 'dialing' | 'incoming' | 'connected'
function callState(){
  try {
    var calls = currentCalls();
    if (!calls.length) return 'none';
    var st = '';
    for (var i = 0; i < calls.length; i++){
      var s = '' + calls[i].callState();
      if (s.indexOf('Connected') >= 0) return 'connected';
      st = s;
    }
    if (st.indexOf('Dialing') >= 0) return 'dialing';
    if (st.indexOf('Incoming') >= 0) return 'incoming';
    return 'active';
  } catch (e) { return 'err:' + e; }
}

function nsconst(m, n){ try { var p = sym(m, n); if (!p) return null; var v = p.readPointer(); return v.isNull() ? null : new ObjC.Object(v); } catch (e) { return null; } }
// NOTE: manipulating AVAudioSession from SpringBoard during a live cellular call
// crashes SpringBoard into Safe Mode (the telephony stack owns the route, our
// setActive fails). Disabled — speakerphone must be toggled via the call UI /
// TUCall instead, and TTS-to-callee needs mic-uplink injection (see README).
function speakerOn(){ return { ok: 0, disabled: 'unsafe in SpringBoard; see README' }; }

function speak(text, lang, rate, volume){
  try {
    if (!G.synth) G.synth = ObjC.classes.AVSpeechSynthesizer.alloc().init();
    var u = ObjC.classes.AVSpeechUtterance.speechUtteranceWithString_(ObjC.classes.NSString.stringWithString_(text));
    var v = ObjC.classes.AVSpeechSynthesisVoice.voiceWithLanguage_(ObjC.classes.NSString.stringWithString_(lang || 'th-TH'));
    if (v && !v.isNull()) u.setVoice_(v);
    u.setRate_(rate || 0.5);
    u.setVolume_(volume == null ? 1.0 : volume);
    G.synth.speakUtterance_(u);
    return { ok: 1 };
  } catch (e) { return { err: '' + e }; }
}

function hangup(){
  try {
    var calls = currentCalls(), n = 0;
    if (CTCallDisconnect){
      for (var i = 0; i < calls.length; i++){ try { CTCallDisconnect(calls[i].handle); n++; } catch (e) {} }
    }
    return { ok: 1, ended: n };
  } catch (e) { return { err: '' + e }; }
}

rpc.exports = {
  dial: function(num){ return dial(num); },
  callState: function(){ return callState(); },
  speakerOn: function(){ return speakerOn(); },
  speak: function(t, l, r, v){ return speak(t, l, r, v); },
  isSpeaking: function(){ try { return !!(G.synth && G.synth.isSpeaking()); } catch (e) { return false; } },
  writeFile: function(p, b){ return writeFile(p, b); },
  playSound: function(p){ return playSound(p); },
  hangup: function(){ return hangup(); },
};
