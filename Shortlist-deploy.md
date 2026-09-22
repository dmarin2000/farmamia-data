Both repos are local-only. Here's the short list to set up the remote:                                                 
                                                                                                                        
 1. GitHub — 2 repos + secrets/variables                                                                                
                                                                                                                        
 Create dmarin/farmamia-data (public = free Actions minutes) and push:                                                  
                                                                                                                        
 ```bash                                                                                                                
   cd /Volumes/Terramaster/progetti/android/farmamia-data                                                               
   git remote add origin git@github.com:dmarin/farmamia-data.git                                                        
   git push -u origin main        # 1 commit: full pipeline                                                             
 ```                                                                                                                    
                                                                                                                        
 (The app repo farmamia has no remote either — push it the same way if you want a backup/CI there.)                     
                                                                                                                        
 Then in farmamia-data settings:                                                                                        
 - Settings → Secrets and variables → Actions → Secrets: R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET 
 - Variables: CATALOG_BASE_URL = the Worker URL from step 2 (e.g. https://catalog.<account>.workers.dev)                
                                                                                                                        
 2. Cloudflare — R2 bucket                                                                                              
                                                                                                                        
 Dashboard → R2 → Create bucket: name farmamia-catalog (any name; it's the secret, not public).                         
 Then Account → R2 → Manage R2 API Tokens → Create token: name gh-actions, permission Admin on that bucket only → gives 
 you the access key ID + secret (the two token secrets) and the endpoint https://<account-id>.r2.cloudflarestorage.com. 
                                                                                                                        
 3. Cloudflare — Worker (reserves the name!)                                                                            
                                                                                                                        
 ```bash                                                                                                                
   npm i -g wrangler && wrangler login                                                                                  
   wrangler worker init catalog && cd catalog                                                                           
 ```                                                                                                                    
                                                                                                                        
 wrangler.jsonc: { "name": "catalog", "main": "src/index.js", "compatibility_date": "2026-09-14" }                      
                                                                                                                        
 Stub (deploy this first — it reserves catalog.<account>.workers.dev against squatting):                                
                                                                                                                        
 ```js                                                                                                                  
   export default { async fetch() { return Response.json({ ok: true, status: "stub" }); } };                            
 ```                                                                                                                    
                                                                                                                        
 wrangler deploy → note the URL https://catalog.<account>.workers.dev → that's your CATALOG_BASE_URL.                   
                                                                                                                        
 Full Worker (replaces the stub — thin passthrough, plan §6.1):                                                         
                                                                                                                        
 ```js                                                                                                                  
   const BUCKET = "farmamia-catalog";                                                                                   
   export default {                                                                                                     
     async fetch(req, env) {                                                                                            
       const url = new URL(req.url);                                                                                    
       if (req.method === "GET") {                                                                                      
         const path = url.pathname.replace(/^\//, "") || "manifest.json";                                               
         const cacheCtrl = path === "manifest.json"                                                                     
           ? "max-age=300, must-revalidate" : "max-age=604800, immutable";                                              
         const up = await fetch(`https://${env.R2_ACCOUNT_ID}.r2.cloudflarestorage.com/${BUCKET}/${path}`);             
         const headers = new Headers(up.headers);                                                                       
         headers.set("cache-control", cacheCtrl);                                                                       
         headers.set("access-control-allow-origin", "*");                                                               
         return new Response(up.body, { status: up.status, headers });                                                  
       }                                                                                                                
       return new Response("method not allowed", { status: 405 });                                                      
     },                                                                                                                 
   };                                                                                                                   
 ```                                                                                                                    
                                                                                                                        
 wrangler secret put R2_ACCOUNT_ID (account ID, read-only — R2 accepts same-account reads). That's the whole P1 server. 
                                                                                                                        
 4. Point the app at it                                                                                                 
                                                                                                                        
 One-line change once the Worker is live: CatalogRemote.DEFAULT_BASE_URL (app/.../data/CatalogRemote.kt) from the       
 farmamia.example placeholder to the real workers.dev URL, rebuild, ship. (The emulator tests keep using 10.0.2.2:8080  
 — already parameterized.)                                                                                              
                                                                                                                        
 5. First real run                                                                                                      
                                                                                                                        
 After all four: gh workflow run catalog-pipeline.yml (or wait for the 06:00 UTC cron) → it fetches sources, diffs vs   
 the published baseline (our dist/ is already staged in the bucket? no — the first publish uploads                      
 baselines/894124ba9081.db + overlay_2 + manifest), and the app updates itself from the real network.                   
                                                                                                                        
 Two gotchas worth knowing: the repo must be public for free unlimited minutes (private = 2,000 min/mo, our run is ~10  
 min, so private works too but burns quota); and the A/H + Mibact source URLs in download_sources.py were probed from   
 this machine — the Actions runner should be fine, but if the AIFA WAF 403s the cron's curl the workflow fails closed   
 and the last publish stays live (by design).                                                         