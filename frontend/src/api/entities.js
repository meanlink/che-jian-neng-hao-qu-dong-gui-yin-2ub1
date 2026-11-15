import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
export const meters={
 status:()=>api('/meters/status'),
 batches:()=>api('/meters/batches'),
 batch:id=>api('/meters/batches/'+id),
 ingest:data=>api('/meters/batches',{method:'POST',body:JSON.stringify(data)}),
 publish:id=>api('/meters/batches/'+id+'/publish',{method:'POST'}),
 rebuild:meterId=>api('/meters/states/'+encodeURIComponent(meterId)+'/rebuild',{method:'POST'}),
 daily:params=>api('/meters/daily'+(params?('?'+new URLSearchParams(params)):'')),
 basis:id=>api('/meters/daily/'+id+'/basis'),
 merge:data=>api('/meters/merge',{method:'POST',body:JSON.stringify(data)}),
};
