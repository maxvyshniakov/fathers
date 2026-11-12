// The demo cannot ask screen-kit's headless terminal for its colours (#14), so each recipe tells the dashboard the
// background and foreground it renders with. The owner's themes (cmux, from its bundled Ghostty theme files):
// dark Seti, light Pro Light, with their exact palettes.
const dark={bg:'#111213',fg:'#cacecd',palette:[
 '#323232','#c22832','#8ec43d','#e0c64f','#43a5d5','#8b57b5','#8ec43d','#eeeeee',
 '#3f3f3f','#c22832','#8ec43d','#e0c64f','#43a5d5','#8b57b5','#8ec43d','#ffffff',
].join(':')};
const light={bg:'#ffffff',fg:'#191919',palette:[
 '#000000','#e5492b','#50d148','#c6c440','#3b75ff','#ed66e8','#4ed2de','#c2c2c2',
 '#9f9f9f','#ff6640','#48d53e','#bfbe23','#0082ff','#ff7eff','#3bd1d2','#f2f2f2',
].join(':')};
const surface=({bg,fg})=>({SLOTFATHER_TERMINAL_BG:bg,SLOTFATHER_TERMINAL_FG:fg,SLOTFATHER_THEME:'auto'});
const terminal={
 source:'terminal',command:['python3','tools/dashboard-demo.py'],columns:176,rows:50,theme:dark,
 env:{NO_COLOR:'',COLORFGBG:'',...surface(dark)},
 fontSize:18,lineHeight:1.2,fontFamily:'Screen Kit Mono,JetBrains Mono,Noto Sans Symbols 2',
};
const lit={theme:light,env:{NO_COLOR:'',COLORFGBG:'',...surface(light)}};
const navigation=[
  {at:.4,input:'j',expect:['label      lane-465-state-definitions-review-fixes','stage      active'],settle:.25},
  {at:2,expect:['label      lane-465-state-definitions-review-fixes','ticket     #465','stage      active'],settle:.25},
  {at:3.7,input:'j',expect:'label      desktop',settle:.25},
  {at:4,input:'j',expect:'waiting · #1 · wants 6',settle:.25},
  {at:4.3,input:'j',expect:['waiting · #2 · wants 1','ticket     #555','stage      active'],settle:.25},
  {at:6.8,input:'\x1b[<65;150;12M'.repeat(4),expect:'claims',settle:.25},
  {at:8.3,input:'g',expect:['label      lane-644-build-once','receipt    in progress'],settle:.25},
  {at:9,input:'j',expect:'label      lane-465-state-definitions-review-fixes',settle:.25},
  {at:9.4,input:'\x1b[<65;150;12M'.repeat(6),expect:'claims',settle:.25},
  {at:10.4,input:'g',expect:['label      lane-644-build-once','stage      active'],settle:.25},
];
export default {cwd:'..',recipes:{
 dashboard:{...terminal,format:'png',duration:1.5,at:1},
 'dashboard-light':{...terminal,...lit,format:'png',duration:1.5,at:1},
 'dashboard-navigation':{...terminal,format:'webp',fps:4,duration:11.5,steps:navigation},
 'dashboard-navigation-light':{...terminal,...lit,format:'webp',fps:4,duration:11.5,steps:navigation},
}};
