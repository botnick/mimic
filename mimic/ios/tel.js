'use strict';
/* MimicTel — telephony + TTS agent, injected into SpringBoard.
 * Places a call, detects when the callee answers (CTCallCenter state), and hangs
 * up. Speaking INTO the call (so the callee hears it) is done from the in-call
 * process via AVSpeechSynthesizer.mixToTelephonyUplink (see agent.js speakUplink) —
 * cellular call audio is baseband-sealed, so an acoustic/loudspeaker relay was a
 * dead end (measured rms=0; see docs/TESTING.md). `speak` here is the local-speaker
 * TTS used by mimic_speak (no call).
 */
function sym(m, n){ return Module.findExportByName(m, n) || Module.findExportByName(null, n); }
var G = {};

var _CTCallDisconnect = sym('CoreTelephony', 'CTCallDisconnect');
var CTCallDisconnect = _CTCallDisconnect ? new NativeFunction(_CTCallDisconnect, 'int', ['pointer']) : null;

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
  speak: function(t, l, r, v){ return speak(t, l, r, v); },
  hangup: function(){ return hangup(); },
};
