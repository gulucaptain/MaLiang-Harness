function(ctx, t, scene, assets, random) {
  const w=ctx.canvas.width, h=ctx.canvas.height;
  ctx.fillStyle='#f4efdf'; ctx.fillRect(0,0,w,h);
  // Seeded paper fibers: the same texture is drawn independently at every timestamp.
  for(let i=0;i<3200;i++) {
    const x=random()*w,y=random()*h;
    ctx.fillStyle=`rgba(80,65,45,${random()*.055})`;
    ctx.fillRect(x,y,random()*2+0.3,random()*1.2+0.3);
  }
  ctx.save(); ctx.scale(w/640,h/480);
  // Distant hills.
  for(let k=0;k<4;k++) {
    ctx.beginPath(); ctx.moveTo(-20,240);
    ctx.bezierCurveTo(110,80+k*17,200,250,320,150+k*18);
    ctx.bezierCurveTo(430,60+k*12,570,200,670,140+k*20);
    ctx.lineTo(670,320);ctx.lineTo(-20,320);ctx.closePath();
    ctx.fillStyle=`rgba(72,83,76,${.028+k*.005})`;ctx.fill();
  }
  // Pond rings.
  for(let i=0;i<7;i++) {
    ctx.beginPath();ctx.ellipse(320,310,180+i*6,65+i*3,0,0,Math.PI*2);
    ctx.strokeStyle=`rgba(76,94,90,${.09-i*.008})`;ctx.lineWidth=.7;ctx.stroke();
  }
  // Bamboo: several irregular bristle passes.
  for(let stalk=0;stalk<3;stalk++) {
    const x=70+stalk*25;
    for(let bristle=0;bristle<7;bristle++) {
      ctx.beginPath();ctx.moveTo(x+random()*4,380);
      ctx.quadraticCurveTo(x+10,200,x+30+random()*5,65);
      ctx.strokeStyle=`rgba(24,49,39,${.12+random()*.10})`;
      ctx.lineWidth=.8+random()*2;ctx.stroke();
    }
    for(let j=0;j<5;j++) {
      const y=110+j*46;
      ctx.save();ctx.translate(x+24-j*3,y);
      ctx.rotate((j%2?-.6:.5));
      ctx.beginPath();ctx.moveTo(0,0);ctx.quadraticCurveTo(34,-27,59,-5);
      ctx.quadraticCurveTo(20,-2,0,0);ctx.fillStyle='rgba(27,52,40,.73)';ctx.fill();ctx.restore();
    }
  }
  const phase=t/scene.spec.duration*Math.PI*2;
  for(let k=0;k<2;k++) {
    const a=phase+k*Math.PI;
    const x=330+Math.cos(a)*100,y=304+Math.sin(a)*33;
    ctx.save();ctx.translate(x,y);ctx.rotate(Math.atan2(Math.cos(a)*33,-Math.sin(a)*100));
    ctx.beginPath();ctx.ellipse(0,0,11,30,0,0,Math.PI*2);
    ctx.fillStyle='#e5ded0';ctx.fill();ctx.strokeStyle='rgba(35,40,35,.45)';ctx.lineWidth=1;ctx.stroke();
    ctx.fillStyle=k?'#303932':'#bd4c35';
    for(let i=0;i<4;i++){ctx.beginPath();ctx.ellipse((i%2?4:-4),-17+i*10,6,8,.4,0,7);ctx.fill();}
    ctx.beginPath();ctx.moveTo(-3,24);ctx.quadraticCurveTo(-21,39,-12,43);
    ctx.lineTo(0,33);ctx.lineTo(15,42);ctx.quadraticCurveTo(20,32,3,24);ctx.fill();
    ctx.fillStyle='#222';ctx.beginPath();ctx.arc(-5,-21,1.5,0,7);ctx.fill();ctx.restore();
  }
  ctx.fillStyle='#39453e';ctx.font='18px serif';ctx.fillText('A quiet pond',350,83);
  ctx.font='10px sans-serif';ctx.fillStyle='#7a8175';ctx.fillText('MALIANG / PROGRAMMABLE STUDY',350,103);
  // Imported/generated assets use the same scene composition contract.
  for(const obj of scene.objects) {
    if(obj.asset_ids.length && assets[obj.asset_ids[0]]) {
      const p=obj.properties;
      ctx.drawImage(assets[obj.asset_ids[0]],p.x||500,p.y||330,p.width||64,p.height||64);
    }
  }
  ctx.strokeStyle='#b64a3b';ctx.lineWidth=2;ctx.strokeRect(560,380,30,32);
  ctx.fillStyle='#b64a3b';ctx.font='bold 16px serif';ctx.fillText('D',569,402);
  ctx.restore();
}
