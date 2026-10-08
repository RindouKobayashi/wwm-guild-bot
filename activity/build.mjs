import {copyFile} from 'node:fs/promises';
await copyFile(new URL('./src/app.js',import.meta.url),new URL('./public/app.js',import.meta.url));
if(process.argv.includes('--preview'))console.log('Local preview built; no Discord SDK required.');
else {
 const {build}=await import('esbuild');
 await build({entryPoints:['src/discord-sdk.js'],bundle:true,format:'esm',outfile:'public/discord-sdk.js'});
 console.log('Activity built with official Discord SDK.');
}
