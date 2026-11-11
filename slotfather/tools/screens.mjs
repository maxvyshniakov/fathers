// The demo cannot ask screen-kit's headless terminal for its colours (#14), so each recipe tells the dashboard the
// background and foreground it renders with. Dark: Ghostty's built-in default (screen-kit's). Light: a warm white
// with GitHub Light's ANSI palette.
const dark={bg:'#282c34',fg:'#ffffff'};
const light={bg:'#fdfcfa',fg:'#2a2a2a',palette:[
 '#24292f','#cf222e','#116329','#4d2d00','#0969da','#8250df','#1b7c83','#6e7781',
 '#57606a','#a40e26','#1a7f37','#633c01','#218bff','#a475f9','#3192aa','#8c959f',
].join(':')};
const surface=({bg,fg})=>({SLOTFATHER_TERMINAL_BG:bg,SLOTFATHER_TERMINAL_FG:fg,SLOTFATHER_THEME:'auto'});
const terminal={
 source:'terminal',command:['python3','tools/dashboard-demo.py'],columns:176,rows:50,
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
  {at:6.8,input:'\x1b[<65;150;12M'.repeat(4),expect:['above','claims'],settle:.25},
  {at:8.3,input:'g',expect:['label      lane-644-build-once','receipt    in progress'],settle:.25},
  {at:9,input:'j',expect:'label      lane-465-state-definitions-review-fixes',settle:.25},
  {at:9.4,input:'\x1b[<65;150;12M'.repeat(6),expect:['above','claims'],settle:.25},
  {at:10.4,input:'g',expect:['label      lane-644-build-once','stage      active'],settle:.25},
];
export default {cwd:'..',recipes:{
 dashboard:{...terminal,format:'png',duration:1.5,at:1},
 'dashboard-light':{...terminal,...lit,format:'png',duration:1.5,at:1},
 'dashboard-navigation':{...terminal,format:'webp',fps:4,duration:11.5,steps:navigation},
 'dashboard-navigation-light':{...terminal,...lit,format:'webp',fps:4,duration:11.5,steps:navigation},
}};
