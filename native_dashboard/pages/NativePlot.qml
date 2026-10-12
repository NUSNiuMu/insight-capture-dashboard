import QtQuick
import QtQuick.Controls
Rectangle {
    id: plot
    property var lines: []
    property var referenceBounds: null
    property real yaw: -0.45
    property real pitch: 0.35
    property real zoom: 1
    color: "#efe8d7"; radius: 9
    implicitHeight: 420
    function reset() { yaw=-0.45;pitch=0.35;zoom=1;canvas.requestPaint() }
    onLinesChanged: canvas.requestPaint()
    onReferenceBoundsChanged: canvas.requestPaint()
    onYawChanged: canvas.requestPaint()
    onPitchChanged: canvas.requestPaint()
    onZoomChanged: canvas.requestPaint()
    Canvas {
        id: canvas; anchors.fill: parent
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        onPaint: {
            var ctx=getContext("2d");ctx.reset();ctx.clearRect(0,0,width,height)
            var all=[];plot.lines.forEach(function(l){all=all.concat(l.points || [])})
            if(!all.length)return
            var min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity]
            if(plot.referenceBounds){min=plot.referenceBounds.min;max=plot.referenceBounds.max}
            else all.forEach(function(p){for(var i=0;i<3;++i){min[i]=Math.min(min[i],p[i]);max[i]=Math.max(max[i],p[i])}})
            var center=min.map(function(v,i){return (v+max[i])/2})
            var scale=Math.min(width,height)*0.7*plot.zoom/Math.max(0.001,max[0]-min[0],max[1]-min[1],max[2]-min[2])
            function project(p) {
                var x=p[0]-center[0],y=p[1]-center[1],z=p[2]-center[2]
                var xx=x*Math.cos(plot.yaw)+z*Math.sin(plot.yaw),zz=-x*Math.sin(plot.yaw)+z*Math.cos(plot.yaw)
                return [width/2+xx*scale,height/2-(y*Math.cos(plot.pitch)-zz*Math.sin(plot.pitch))*scale]
            }
            plot.lines.forEach(function(l){
                var pts=(l.points || []).map(project);ctx.strokeStyle=l.color || "#1f8a55";ctx.fillStyle=ctx.strokeStyle;ctx.lineWidth=2
                var edges=l.edges || [];ctx.beginPath()
                if(edges.length)edges.forEach(function(e){if(pts[e[0]] && pts[e[1]]){ctx.moveTo(pts[e[0]][0],pts[e[0]][1]);ctx.lineTo(pts[e[1]][0],pts[e[1]][1])}})
                else pts.forEach(function(p,i){if(i)ctx.lineTo(p[0],p[1]);else ctx.moveTo(p[0],p[1])})
                ctx.stroke()
                if(l.joints)pts.forEach(function(p){ctx.beginPath();ctx.arc(p[0],p[1],3,0,2*Math.PI);ctx.fill()})
            })
        }
    }
    MouseArea {
        anchors.fill: parent; property real lastX; property real lastY
        onPressed: function(mouse){lastX=mouse.x;lastY=mouse.y}
        onPositionChanged: function(mouse){if(!pressed)return;plot.yaw+=(mouse.x-lastX)*0.01;plot.pitch+=(mouse.y-lastY)*0.01;lastX=mouse.x;lastY=mouse.y}
        onWheel: function(wheel){plot.zoom=Math.max(0.1,Math.min(10,plot.zoom*(wheel.angleDelta.y>0 ? 1.1 : 0.9)))}
        onDoubleClicked: plot.reset()
    }
    Label { anchors.bottom: parent.bottom; anchors.left: parent.left; anchors.margins: 12; text: "拖动旋转 · 滚轮缩放 · 双击复位"; color: "#776d5a" }
}
