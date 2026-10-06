/* View-owned finite callbacks. SessionController remains the session owner. */
class UiLifetime {
  constructor(){ this._generation=0; this._timeouts=new Set(); this._frames=new Set(); this._requests=new Map(); this._abort=new AbortController(); this._cleanups=new Set(); }
  generation(){ return this._generation; }
  isCurrent(token){ return typeof token==='number' ? token===this._generation : token.generation===this._generation && (!token.key || this._requests.get(token.key)===token.sequence); }
  capture(key){ const sequence=key ? (this._requests.get(key)||0)+1 : 0; if(key)this._requests.set(key,sequence);return {generation:this._generation,key,sequence}; }
  assertCurrent(token){ if(!this.isCurrent(token)){const error=new Error('View request is stale');error.name='AbortError';error.staleView=true;throw error;} }
  async result(promise,token){
    try { const value=await promise;this.assertCurrent(token);return value; }
    catch(error){ this.assertCurrent(token);throw error; }
  }
  invalidate(key){ this._requests.set(key,(this._requests.get(key)||0)+1); }
  action(operation){ return async (...args)=>{try{return await operation(...args);}catch(error){if(error.staleView)return false;throw error;}}; }
  requestOptions(options={}){ return {...options,signal:options.signal ? AbortSignal.any([options.signal,this._abort.signal]) : this._abort.signal}; }
  ownCleanup(callback){ this._cleanups.add(callback);return ()=>{if(this._cleanups.delete(callback))callback();}; }
  setTimeout(callback, delay){
    const generation=this._generation;
    const timer=globalThis.setTimeout(()=>{this._timeouts.delete(timer);if(this.isCurrent(generation))callback();},delay);
    this._timeouts.add(timer);return timer;
  }
  clearTimeout(timer){ globalThis.clearTimeout(timer);this._timeouts.delete(timer); }
  requestAnimationFrame(callback){
    const generation=this._generation;
    const frame=globalThis.requestAnimationFrame(time=>{this._frames.delete(frame);if(this.isCurrent(generation))callback(time);});
    this._frames.add(frame);return frame;
  }
  dispose(){
    this._generation+=1;
    this._abort.abort();this._abort=new AbortController();this._requests.clear();
    this._timeouts.forEach(timer=>globalThis.clearTimeout(timer));this._timeouts.clear();
    this._frames.forEach(frame=>globalThis.cancelAnimationFrame(frame));this._frames.clear();
    this._cleanups.forEach(callback=>callback());this._cleanups.clear();
  }
}
