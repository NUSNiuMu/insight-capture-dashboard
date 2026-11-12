const fs = require('fs');
const path = require('path');
const { build } = require('esbuild');
const { parse, compileScript } = require('@vue/compiler-sfc');
const dist = path.join(__dirname, 'dist');
fs.rmSync(dist, {recursive:true, force:true});
fs.mkdirSync(dist, {recursive:true});
fs.copyFileSync(path.join(__dirname,'src/mobile/index.html'),path.join(dist,'mobile.html'));
build({entryPoints:[path.join(__dirname,'src/mobile/app.js')],bundle:true,minify:true,
  format:'esm',outfile:path.join(dist,'static/mobile.js'),
  define:{'process.env.NODE_ENV':'"production"',__VUE_OPTIONS_API__:'false',__VUE_PROD_DEVTOOLS__:'false',__VUE_PROD_HYDRATION_MISMATCH_DETAILS__:'false'},
  plugins:[{name:'vue',setup(builder){builder.onLoad({filter:/\.vue$/},args=>{
    const {descriptor}=parse(fs.readFileSync(args.path,'utf8'),{filename:args.path});
    const result=compileScript(descriptor,{id:path.basename(args.path),inlineTemplate:true});
    return {contents:result.content,loader:'js',resolveDir:path.dirname(args.path)};
  });}}],
}).catch(error=>{console.error(error);process.exit(1)});
