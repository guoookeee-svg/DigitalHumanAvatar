/**
 * Copyright FunASR (https://github.com/alibaba-damo-academy/FunASR). All Rights
 * Reserved. MIT License  (https://opensource.org/licenses/MIT)
 */
/* 2022-2023 by zhaoming,mali aihealthx.com */


// 连接; 定义socket连接类对象与语音对象
var wsconnecter = new WebSocketConnectMethod({msgHandle:getJsonMessage,stateHandle:getConnState});
var audioBlob;

// 录音; 定义录音对象,wav格式
var rec = Recorder({
	type:"pcm",
	bitRate:16,
	sampleRate:16000,
	onProcess:recProcess,
	// 参考 VoxEMW：启用回声消除/降噪/自动增益，避免麦克风录到数字人自己的声音触发打断
	audioTrackSet:{
		echoCancellation:true,
		noiseSuppression:true,
		autoGainControl:true
	}
});

 
 
 
var sampleBuf=new Int16Array();
// 定义按钮响应事件
var btnStart = document.getElementById('btnStart');
btnStart.onclick = record;
var btnStop = document.getElementById('btnStop');
btnStop.onclick = stop;
btnStop.disabled = true;
btnStart.disabled = true;
 
btnConnect= document.getElementById('btnConnect');
btnConnect.onclick = start;

var awsslink= document.getElementById('wsslink');

 
var rec_text="";  // for online rec asr result
var offline_text=""; // for offline rec asr result
var info_div = document.getElementById('info_div');
var lastPowerLevel = 0;  // 最近一次录音音量（用于打断检测）
var manualStopped = false;  // 用户手动停止标志：停止后不自动重新录音

var upfile = document.getElementById('upfile');

 

var isfilemode=false;  // if it is in file mode
var file_ext="";
var file_sample_rate=16000; //for wav file sample rate
var file_data_array;  // array to save file data
 
var totalsend=0;


// Auto-detect local ASR server endpoint (Issue #604: SenseVoice integration)
// Falls back to the input field's default value if detection fails.
(function() {
	try {
		var protocol = window.location.protocol === "https:" ? "wss://" : "ws://";
		// 优先用父页面的 host（iframe 嵌入时父页面才是真实的访问地址）
		var host = (parent && parent.location && parent.location.host) || window.location.host;
		document.getElementById('wssip').value = protocol + host + "/api/asr";
		console.log("[ASR] Auto-detected local endpoint: " + protocol + host + "/api/asr");
	} catch(e) {
		console.warn("[ASR] Auto-detect failed, using default address", e);
	}
	// 设置默认热词：提高"数字人"等关键术语的识别率
	try {
		var hotEl = document.getElementById('varHot');
		if (hotEl && (!hotEl.value || hotEl.value.indexOf('数字人') === -1)) {
			hotEl.value = '数字人 50\n总结 30\n机器人 20';
		}
	} catch(e) {
		console.warn("[ASR] 设置默认热词失败", e);
	}
})();
addresschange();
function addresschange()
{   
	
    var Uri = document.getElementById('wssip').value; 
	document.getElementById('info_wslink').innerHTML="点此处手工授权（IOS手机）";
	Uri=Uri.replace(/wss/g,"https");
	console.log("addresschange uri=",Uri);
	
	awsslink.onclick=function(){
		window.open(Uri, '_blank');
		}
	
}

upfile.onclick=function()
{
		btnStart.disabled = true;
		btnStop.disabled = true;
		btnConnect.disabled=false;
	
}

// from https://github.com/xiangyuecn/Recorder/tree/master
var readWavInfo=function(bytes){
	//读取wav文件头，统一成44字节的头
	if(bytes.byteLength<44){
		return null;
	};
	var wavView=bytes;
	var eq=function(p,s){
		for(var i=0;i<s.length;i++){
			if(wavView[p+i]!=s.charCodeAt(i)){
				return false;
			};
		};
		return true;
	};
	
	if(eq(0,"RIFF")&&eq(8,"WAVEfmt ")){
 
		var numCh=wavView[22];
		if(wavView[20]==1 && (numCh==1||numCh==2)){//raw pcm 单或双声道
			var sampleRate=wavView[24]+(wavView[25]<<8)+(wavView[26]<<16)+(wavView[27]<<24);
			var bitRate=wavView[34]+(wavView[35]<<8);
			var heads=[wavView.subarray(0,12)],headSize=12;//head只保留必要的块
			//搜索data块的位置
			var dataPos=0; // 44 或有更多块
			for(var i=12,iL=wavView.length-8;i<iL;){
				if(wavView[i]==100&&wavView[i+1]==97&&wavView[i+2]==116&&wavView[i+3]==97){//eq(i,"data")
					heads.push(wavView.subarray(i,i+8));
					headSize+=8;
					dataPos=i+8;break;
				}
				var i0=i;
				i+=4;
				i+=4+wavView[i]+(wavView[i+1]<<8)+(wavView[i+2]<<16)+(wavView[i+3]<<24);
				if(i0==12){//fmt 
					heads.push(wavView.subarray(i0,i));
					headSize+=i-i0;
				}
			}
			if(dataPos){
				var wavHead=new Uint8Array(headSize);
				for(var i=0,n=0;i<heads.length;i++){
					wavHead.set(heads[i],n);n+=heads[i].length;
				}
				return {
					sampleRate:sampleRate
					,bitRate:bitRate
					,numChannels:numCh
					,wavHead44:wavHead
					,dataPos:dataPos
				};
			};
		};
	};
	return null;
};

upfile.onchange = function () {
　　　　　　var len = this.files.length;  
            for(let i = 0; i < len; i++) {

                let fileAudio = new FileReader();
                fileAudio.readAsArrayBuffer(this.files[i]);  
 
				file_ext=this.files[i].name.split('.').pop().toLowerCase();
                var audioblob;
                fileAudio.onload = function() {
                audioblob = fileAudio.result;
 
				 
				 file_data_array=audioblob;
 
                  
                 info_div.innerHTML='请点击连接进行识别';
 
                }

　　　　　　　　　　fileAudio.onerror = function(e) {
　　　　　　　　　　　　console.log('error' + e);
　　　　　　　　　　}
            }
			// for wav file, we  get the sample rate
			if(file_ext=="wav")
            for(let i = 0; i < len; i++) {

                let fileAudio = new FileReader();
                fileAudio.readAsArrayBuffer(this.files[i]);  
                fileAudio.onload = function() {
                audioblob = new Uint8Array(fileAudio.result);
 
				// for wav file, we can get the sample rate
				var info=readWavInfo(audioblob);
				   console.log(info);
				   file_sample_rate=info.sampleRate;
	 
 
                }

　　　　　　 
            }
 
        }

function play_file()
{
		  var audioblob=new Blob( [ new Uint8Array(file_data_array)] , {type :"audio/wav"});
		  var audio_record = document.getElementById('audio_record');
		  audio_record.src =  (window.URL||webkitURL).createObjectURL(audioblob); 
          audio_record.controls=true;
		  //audio_record.play();  //not auto play
}
function start_file_send()
{
		sampleBuf=new Uint8Array( file_data_array );
 
		var chunk_size=960; // for asr chunk_size [5, 10, 5]
 

 
		
 
		while(sampleBuf.length>=chunk_size){
			
		    sendBuf=sampleBuf.slice(0,chunk_size);
			totalsend=totalsend+sampleBuf.length;
			sampleBuf=sampleBuf.slice(chunk_size,sampleBuf.length);
			wsconnecter.wsSend(sendBuf);
 
		 
		}
 
		stop();

 

}
 
	
function on_recoder_mode_change()
{
            var item = null;
            var obj = document.getElementsByName("recoder_mode");
            for (var i = 0; i < obj.length; i++) { //遍历Radio 
                if (obj[i].checked) {
                    item = obj[i].value;  
					break;
                }
		    

           }
		    if(item=="mic")
			{
				document.getElementById("mic_mode_div").style.display = 'block';
				document.getElementById("rec_mode_div").style.display = 'none';
 
 
		        btnStart.disabled = true;
		        btnStop.disabled = true;
		        btnConnect.disabled=false;
				isfilemode=false;
			}
			else
			{
				document.getElementById("mic_mode_div").style.display = 'none';
				document.getElementById("rec_mode_div").style.display = 'block';
 
		        btnStart.disabled = true;
		        btnStop.disabled = true;
		        btnConnect.disabled=true;
			    isfilemode=true;
				info_div.innerHTML='请点击选择文件';
			    
	 
			}
}


function getHotwords(){
	
	var obj = document.getElementById("varHot");

	if(typeof(obj) == 'undefined' || obj==null || obj.value.length<=0){
	  return null;
	}
	let val = obj.value.toString();
  
	console.log("hotwords="+val);
	let items = val.split(/[(\r\n)\r\n]+/);  //split by \r\n
	var jsonresult = {};
	const regexNum = /^[0-9]*$/; // test number
	for (item of items) {
  
		let result = item.split(" ");
		if(result.length>=2 && regexNum.test(result[result.length-1]))
		{ 
			var wordstr="";
			for(var i=0;i<result.length-1;i++)
				wordstr=wordstr+result[i]+" ";
  
			jsonresult[wordstr.trim()]= parseInt(result[result.length-1]);
		}
	}
	console.log("jsonresult="+JSON.stringify(jsonresult));
	return  JSON.stringify(jsonresult);

}
function getAsrMode(){

            var item = null;
            var obj = document.getElementsByName("asr_mode");
            for (var i = 0; i < obj.length; i++) { //遍历Radio 
                if (obj[i].checked) {
                    item = obj[i].value;  
					break;
                }
		    

           }
            if(isfilemode)
			{
				item= "offline";
			}
		   console.log("asr mode"+item);
		   
		   return item;
}
		   
function handleWithTimestamp(tmptext,tmptime)
{
	console.log( "tmptext: " + tmptext);
	console.log( "tmptime: " + tmptime);
    if(tmptime==null || tmptime=="undefined" || tmptext.length<=0)
	{
		return tmptext;
	}
	tmptext=tmptext.replace(/。|？|，|、|\?|\.|\ /g, ","); // in case there are a lot of "。"
	var words=tmptext.split(",");  // split to chinese sentence or english words
	var jsontime=JSON.parse(tmptime); //JSON.parse(tmptime.replace(/\]\]\[\[/g, "],[")); // in case there are a lot segments by VAD
	var char_index=0; // index for timestamp
	var text_withtime="";
	for(var i=0;i<words.length;i++)
	{   
	if(words[i]=="undefined"  || words[i].length<=0)
	{
		continue;
	}
    console.log("words===",words[i]);
	console.log( "words: " + words[i]+",time="+jsontime[char_index][0]/1000);
	if (/^[a-zA-Z]+$/.test(words[i]))
	{   // if it is english
		text_withtime=text_withtime+jsontime[char_index][0]/1000+":"+words[i]+"\n";
		char_index=char_index+1;  //for english, timestamp unit is about a word
	}
	else{
        // if it is chinese
		text_withtime=text_withtime+jsontime[char_index][0]/1000+":"+words[i]+"\n";
		char_index=char_index+words[i].length; //for chinese, timestamp unit is about a char
	}
	}
	return text_withtime;
	

}

const sleep = (delay) => new Promise((resolve) => setTimeout(resolve, delay))
async function is_speaking() {
	const response = await fetch('/is_speaking', {
		body: JSON.stringify({
			sessionid: String(parent.document.getElementById('sessionid').value),
		}),
		headers: {
			'Content-Type': 'application/json'
		},
		method: 'POST'
	  });
	const data = await response.json();
	console.log('is_speaking res:',data)
	return data.data
}

// 自动重新开始录音（VAD 自动判停模式下，数字人说完后自动继续听）
function autoRestartRecord() {
	// 用户手动停止后，不自动重新录音（需重新点"连接"）
	if(manualStopped === true){
		console.log("已手动停止，不自动重新录音");
		return;
	}
	// 问题5：如果已经在录音，不重复 rec.open()（避免重新获取麦克风流）
	if(isRec === true){
		console.log("已在录音中，跳过 rec.open");
		return;
	}
	try {
		rec.open(function(){
			try {
				rec.start();
				console.log("自动重新开始录音");
				btnStart.disabled = true;
				btnStop.disabled = false;
				btnConnect.disabled=true;
			} catch(e) {
				console.error("自动录音 start 失败，2秒后重试", e);
				setTimeout(autoRestartRecord, 2000);
			}
		}, function(err) {
			// rec.open 失败回调（部分浏览器实现有 error callback）
			console.error("自动录音 open 失败，2秒后重试", err);
			setTimeout(autoRestartRecord, 2000);
		});
	} catch(e) {
		console.error("autoRestartRecord 异常，2秒后重试", e);
		setTimeout(autoRestartRecord, 2000);
	}
}

// 打断数字人说话
function interruptAvatar() {
	fetch('/interrupt_talk', {
		body: JSON.stringify({
			sessionid: String(parent.document.getElementById('sessionid').value),
		}),
		headers: {
			'Content-Type': 'application/json'
		},
		method: 'POST'
	}).then(function(resp){
		console.log('interrupt_talk sent');
	});
}

async function waitSpeakingEnd() {
	// 数字人说话时保持录音（不停止），以便随时打断
	// 等待数字人开始讲话，最长等待10s
	for(let i=0;i<10;i++) {
		try { bspeak = await is_speaking() } catch(e) { bspeak = false; }
		if(bspeak) {
			break
		}
		await sleep(1000)
	}

	// 数字人说话期间：保持录音。
	// 打断检测已改由后端负责（asr_server 的 VAD 用户开口检测 _maybe_interrupt
	// + 声纹回声滤除 is_self），前端不再做音量打断
	var dbgCount = 0;
	// 最多等 60 秒（120 次 * 500ms），防止异常情况下无限等待
	var maxLoops = 120;
	while(maxLoops-- > 0) {
		try { bspeak = await is_speaking() } catch(e) { bspeak = false; }
		if(!bspeak) {
			console.log('[打断检测] 数字人已停止说话，退出等待');
			break
		}
		dbgCount++;
		if(dbgCount % 4 === 0) {
			console.log('[打断检测] 数字人说话中（后端VAD+声纹负责打断判定）');
		}
		await sleep(500)
	}
	// VAD 自动判停模式：立即重新开始录音（不再等 2 秒，避免错过用户下一句话）
	autoRestartRecord()
}
// 语音识别结果; 对jsonMsg数据解析,将识别结果附加到编辑框中
function getJsonMessage( jsonMsg ) {
	//console.log(jsonMsg);
	console.log( "message: " + JSON.parse(jsonMsg.data)['text'] );
	var rectxt=""+JSON.parse(jsonMsg.data)['text'];
	var asrmodel=JSON.parse(jsonMsg.data)['mode'];
	var is_final=JSON.parse(jsonMsg.data)['is_final'];
	var timestamp=JSON.parse(jsonMsg.data)['timestamp'];
	// 音频留存路径：后端把这段语音落盘后返回的相对路径，透传给 /human
	var audioPath=JSON.parse(jsonMsg.data)['audio']||"";
	// 实时说话人判定结果
	var spkName=JSON.parse(jsonMsg.data)['speaker']||"";
	var spkConf=JSON.parse(jsonMsg.data)['speaker_conf']||0;
	// is_self=true 表示这段其实是数字人自己的声音（扬声器回灌），不能送 LLM
	var isSelf=JSON.parse(jsonMsg.data)['is_self']===true;
	if(asrmodel=="2pass-offline" || asrmodel=="offline")
	{
		// 识别结果为空时提示
		if(rectxt.replace(/ +/g,"")==""){
			info_div.innerHTML='未识别到语音，请靠近麦克风清晰说话';
			// 识别为空：不等待数字人，直接重新录音
			autoRestartRecord();
			return;
		}
		// 数字人自己的声音（回声被 ASR 识别到）：不送 LLM，直接继续听
		// 这是回声自我打断的根治手段，替代效果有限的浏览器 echoCancellation
		if(isSelf){
			console.log('[说话人] 滤除数字人自身声音: ' + rectxt);
			info_div.innerHTML='（已滤除数字人自身声音，继续聆听）';
			autoRestartRecord();
			return;
		}
		offline_text=offline_text+rectxt.replace(/ +/g,"")+'\n'; //handleWithTimestamp(rectxt,timestamp); //rectxt; //.replace(/ +/g,"");
		rec_text=offline_text;
		// 只有识别到文字才调 /human（避免空文本触发 LLM）
		if(rectxt.replace(/ +/g,"")!=""){
			// 拿到父页面当前选中的会话 ID（如果存在），直接告诉后端该写进哪个会话
			var parentConvId = parent.document.getElementById('sessionid')
				? parent.document.getElementById('convid')?.value || ''
				: '';
			fetch('/human', {
	            body: JSON.stringify({
	                text: rectxt.replace(/ +/g,""),
	                type: 'chat',
	                interrupt: true,  // 问题5：新问题打断旧回复
					sessionid: String(parent.document.getElementById('sessionid').value),
					// 明确指定会话 ID，绕过后端指针表查找，避免因指针不同步而新建会话
					conversation_id: parentConvId,
					// 音频留存路径：后端用它把语音文件关联到这条消息（说话人分离用）
					audio: audioPath,
					// 实时说话人判定结果，写进消息记录
					speaker: spkName,
					speaker_conf: spkConf,
	            }),
	            headers: {
	                'Content-Type': 'application/json'
	            },
	            method: 'POST'
	      	});
		}
		waitSpeakingEnd();
	}
	else
	{
		// online 模式：partial 结果实时显示（2-pass 第一遍）
		rec_text=rectxt; //.replace(/ +/g,"");
		var varAreaPartial=document.getElementById('varArea');
		varAreaPartial.value=rec_text;
	}
	var varArea=document.getElementById('varArea');
	
	varArea.value=rec_text;
	console.log( "offline_text: " + asrmodel+","+offline_text);
	console.log( "rec_text: " + rec_text);
	if (isfilemode==true && is_final==true){
		console.log("call stop ws!");
		play_file();
		wsconnecter.wsStop();
        
		info_div.innerHTML="请点击连接";
 
		btnStart.disabled = true;
		btnStop.disabled = true;
		btnConnect.disabled=false;
	}
	
	 

}

// 连接状态响应
function getConnState( connState ) {
	if ( connState === 0 ) { //on open


		info_div.innerHTML='连接成功!请点击开始';
		if (isfilemode==true){
			info_div.innerHTML='请耐心等待,大文件等待时间更长';
			start_file_send();
		}
		else
		{
			// VAD 自动判停模式：连接成功后自动开始录音
			btnStart.disabled = false;
			btnStop.disabled = true;
			btnConnect.disabled=true;
			info_div.innerHTML='连接成功!自动开始录音,说话停顿后自动识别';
			record();
		}
	} else if ( connState === 1 ) {
		//stop();
	} else if ( connState === 2 ) {
		stop();
		console.log( 'connecttion error' );
		 
		alert("连接地址"+document.getElementById('wssip').value+"失败,请检查asr地址和端口。或试试界面上手动授权，再连接。");
		btnStart.disabled = true;
		btnStop.disabled = true;
		btnConnect.disabled=false;


		info_div.innerHTML='请点击连接';
	}
}

function record()
{
 
		 rec.open( function(){
		 rec.start();
		 console.log("开始");
			btnStart.disabled = true;
			btnStop.disabled = false;
			btnConnect.disabled=true;
		 });
 
}

 

// 识别启动、停止、清空操作
function start() {
	
	// 清除显示
	clear();
	// 用户重新连接：重置手动停止标志，允许自动重新录音
	manualStopped = false;
	//控件状态更新
 	console.log("isfilemode"+isfilemode);
    
	//启动连接
	var ret=wsconnecter.wsStart();
	// 1 is ok, 0 is error
	if(ret==1){
		info_div.innerHTML="正在连接asr服务器，请等待...";
		isRec = true;
		btnStart.disabled = true;
		btnStop.disabled = true;
		btnConnect.disabled=true;
 
        return 1;
	}
	else
	{
		info_div.innerHTML="请点击开始";
		btnStart.disabled = true;
		btnStop.disabled = true;
		btnConnect.disabled=false;
 
		return 0;
	}
}

 
function stop() {
		var chunk_size = new Array( 5, 10, 5 );
		var request = {
			"chunk_size": chunk_size,
			"wav_name":  "h5",
			"is_speaking":  false,
			"chunk_interval":10,
			"mode":getAsrMode(),
		};
		console.log(request);
		if(sampleBuf.length>0){
		wsconnecter.wsSend(sampleBuf);
		console.log("sampleBuf.length"+sampleBuf.length);
		sampleBuf=new Int16Array();
		}
	   wsconnecter.wsSend( JSON.stringify(request) );
 
	  
	
	 

 
	// 控件状态更新
	
	isRec = false;
	manualStopped = true;  // 用户手动停止：后续不自动重新录音
    info_div.innerHTML="发送完数据,请等候,正在识别...";

   if(isfilemode==false){
	    btnStop.disabled = true;
		btnStart.disabled = true;
		btnConnect.disabled=true;
		//wait 3s for asr result
	  setTimeout(function(){
		console.log("call stop ws!");
		wsconnecter.wsStop();
		btnConnect.disabled=false;
		info_div.innerHTML="请点击连接";}, 3000 );
 
 
	   
	rec.stop(function(blob,duration){
  
		console.log(blob);
		var audioBlob = Recorder.pcm2wav(data = {sampleRate:16000, bitRate:16, blob:blob},
		function(theblob,duration){
				console.log(theblob);
		var audio_record = document.getElementById('audio_record');
		audio_record.src =  (window.URL||webkitURL).createObjectURL(theblob); 
        audio_record.controls=true;
		//audio_record.play(); 
         	

	}   ,function(msg){
		 console.log(msg);
	}
		);
 

 
	},function(errMsg){
		console.log("errMsg: " + errMsg);
	});
   }
    // 停止连接
 
    

}

function clear() {
 
    var varArea=document.getElementById('varArea');
 
	varArea.value="";
    rec_text="";
	offline_text="";
 
}

 
function recProcess( buffer, powerLevel, bufferDuration, bufferSampleRate,newBufferIdx,asyncEnd ) {
	if ( isRec === true ) {
		// 记录当前音量（用于打断检测）
		lastPowerLevel = powerLevel;
		var data_48k = buffer[buffer.length-1];  
 
		var  array_48k = new Array(data_48k);
		var data_16k=Recorder.SampleData(array_48k,bufferSampleRate,16000).data;

		sampleBuf = Int16Array.from([...sampleBuf, ...data_16k]);
		var chunk_size=960; // for asr chunk_size [5, 10, 5]
		// 音量检测：powerLevel 是音量电平（0-100），太低提示麦克风问题
		if(powerLevel < 5){
			info_div.innerHTML=""+bufferDuration/1000+"s (音量过低，请靠近麦克风)";
		} else {
			info_div.innerHTML=""+bufferDuration/1000+"s";
		}
		// 音频增益放大：提高音量，改善识别率。
		// 1.5 增益下数字人扬声器回灌仍会小幅度突破 VAD 阈值 0.3，
		// 进一步降至 1.2 在音量与回声抑制间取平衡。
		var GAIN = 1.2;  // 增益倍数
		while(sampleBuf.length>=chunk_size){
		    sendBuf=sampleBuf.slice(0,chunk_size);
			sampleBuf=sampleBuf.slice(chunk_size,sampleBuf.length);
			// 增益放大（int16，防溢出）
			var gainBuf = new Int16Array(sendBuf.length);
			for(var gi=0; gi<sendBuf.length; gi++){
				var v = sendBuf[gi] * GAIN;
				if(v > 32767) v = 32767;
				if(v < -32768) v = -32768;
				gainBuf[gi] = v;
			}
			wsconnecter.wsSend(gainBuf.buffer);
			
			
		 
		}
		
 
		
	}
}

function getUseITN() {
	var obj = document.getElementsByName("use_itn");
	for (var i = 0; i < obj.length; i++) {
		if (obj[i].checked) {
			return obj[i].value === "true";
		}
	}
	return false;
}
