/* Offline poster renderer. No file upload, image provider, model, or external font. */
(() => {
  'use strict';
  const WIDTH = 1000, HEIGHT = 1414, MARGIN = 80, CONTENT = WIDTH - MARGIN * 2;
  const OUTPUT_WIDTH = 2480, OUTPUT_HEIGHT = 3508, SCALE = OUTPUT_WIDTH / WIDTH;
  const LABELS = {program:'프로그램',audience:'대상',date:'날짜',time:'시간',place:'장소',price:'비용',contact:'문의'};
  const FIELDS = ['title','goal','program','audience','date','time','place','price','contact'];
  function value(payload,key) {const raw=payload?.[key];return raw===null||raw===undefined||String(raw).trim()===''?'[확인 필요]':String(raw);}
  function font(size,weight=400) {return `${weight} ${size}px "Malgun Gothic", "Apple SD Gothic Neo", sans-serif`;}
  function linesFor(ctx,text,maxWidth) {
    const lines=[];
    for(const paragraph of String(text).split(/\r?\n/)) {
      let line='';
      for(const char of Array.from(paragraph)) {
        if(ctx.measureText(char).width>maxWidth)throw new Error('이 글자를 용지에 표시할 수 없어요. 문구를 확인해 주세요.');
        if(line&&ctx.measureText(line+char).width>maxWidth){lines.push(line);line=char;}else line+=char;
      }
      lines.push(line);
    }
    return lines;
  }
  async function create(payload) {
    if(!String(payload?.title||'').trim())throw new Error('홍보물 제목을 입력해 주세요.');
    if(document.fonts?.ready)await document.fonts.ready;
    const canvas=document.createElement('canvas');canvas.width=OUTPUT_WIDTH;canvas.height=OUTPUT_HEIGHT;
    const ctx=canvas.getContext('2d');if(!ctx)throw new Error('이 브라우저에서 PNG 그림을 만들 수 없어요.');
    ctx.setTransform(SCALE,0,0,SCALE,0,0);
    const texts=Object.fromEntries(FIELDS.map(key=>[key,value(payload,key)])),boxes=[];
    let y=MARGIN;
    function add(key,text,size,gap=16,weight=400) {
      ctx.font=font(size,weight);const lines=linesFor(ctx,text,CONTENT),lineHeight=Math.ceil(size*1.36),height=lines.length*lineHeight;
      const widths=lines.map(line=>ctx.measureText(line).width);
      boxes.push({key,text,lines,x:MARGIN,y,width:Math.max(0,...widths),height,font_size:size,line_height:lineHeight,weight});
      y+=height+gap;
    }
    add('intro','참여 안내 · 확인용 초안',24,18,500);
    add('title',texts.title,52,18,700);
    add('goal',texts.goal,32,22);
    add('program',LABELS.program+': '+texts.program,32,10,600);
    add('audience',LABELS.audience+': '+texts.audience,32,22);
    const photoBox={key:'photo_placeholder',x:MARGIN,y,width:CONTENT,height:106};y+=128;
    for(const key of ['date','time','place','price','contact'])add(key,LABELS[key]+': '+texts[key],32,10);
    add('footer','내용 확인이 필요한 홍보 시안 · 사진 미첨부',24,0);
    if(y>HEIGHT-MARGIN) {
      const err=new Error('문구가 길어 한 장에 모두 들어가지 않아요. 제목·목적·참여 안내를 직접 짧게 정리한 뒤 다시 눌러 주세요. 글씨를 줄이거나 내용을 자동으로 버리지 않았습니다. 원래 입력은 그대로 남아 있어요.');
      err.code='poster_overflow';err.required_height=y+MARGIN;err.page_height=HEIGHT;throw err;
    }
    ctx.fillStyle='#f7f4e9';ctx.fillRect(0,0,WIDTH,OUTPUT_HEIGHT/SCALE);
    ctx.fillStyle='#244b3a';ctx.fillRect(0,0,WIDTH,18);
    ctx.fillStyle='#e3eade';ctx.fillRect(photoBox.x,photoBox.y,photoBox.width,photoBox.height);
    ctx.strokeStyle='#8b9c89';ctx.lineWidth=2;ctx.strokeRect(photoBox.x,photoBox.y,photoBox.width,photoBox.height);
    ctx.textBaseline='top';ctx.font=font(30,500);ctx.fillStyle='#597064';
    const photoText='제품 사진 자리 · 사진 미첨부';ctx.fillText(photoText,MARGIN+28,photoBox.y+34);
    for(const box of boxes) {
      ctx.font=font(box.font_size,box.weight);ctx.fillStyle=box.key==='intro'||box.key==='footer'?'#5f705f':'#23483b';
      box.lines.forEach((line,index)=>ctx.fillText(line,box.x,box.y+index*box.line_height));
    }
    const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));if(!blob)throw new Error('PNG 파일을 만들지 못했어요.');
    return {blob,width:OUTPUT_WIDTH,height:OUTPUT_HEIGHT,logical_width:WIDTH,logical_height:HEIGHT,scale:SCALE,texts,boxes,photo_box:photoBox,min_font_px:24,body_font_px:32,output_min_font_px:24*SCALE,output_body_font_px:32*SCALE,color_mode:'RGB',dpi_metadata:false,ai_used:false,image_generation:false,photo_attached:false};
  }
  globalThis.AromaPromoPng=Object.freeze({create,width:OUTPUT_WIDTH,height:OUTPUT_HEIGHT,logical_width:WIDTH,logical_height:HEIGHT,public_fields:FIELDS.slice()});
})();
