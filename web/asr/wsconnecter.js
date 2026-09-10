/**
 * Copyright FunASR (https://github.com/alibaba-damo-academy/FunASR). All Rights
 * Reserved. MIT License  (https://opensource.org/licenses/MIT)
 */
/* 2021-2023 by zhaoming,mali aihealthx.com */

function WebSocketConnectMethod( config ) { //定义socket连接方法类

	
	var speechSokt;
	var connKeeperID;
	var reconnectTimer = null;
	
	var msgHandle = config.msgHandle;
	var stateHandle = config.stateHandle;
			  
	this.wsStart = function () {
		var Uri = document.getElementById('wssip').value; //"wss://111.205.137.58:5821/wss/" //设置wss asr online接口地址 如 wss://X.X.X.X:port/wss/
		if(Uri.match(/wss:\S*|ws:\S*/))
		{
			console.log("Uri"+Uri);
		}
		else
		{
			alert("请检查wss地址正确性");
			return 0;
		}
 
		if ( 'WebSocket' in window ) {
			speechSokt = new WebSocket( Uri ); // 定义socket连接对象
			speechSokt.onopen = function(e){onOpen(e);}; // 定义响应函数
			speechSokt.onclose = function(e){
			    console.log("onclose ws!");
			    //speechSokt.close();
				onClose(e);
				};
			speechSokt.onmessage = function(e){onMessage(e);};
			speechSokt.onerror = function(e){onError(e);};
			return 1;
		}
		else {
			alert('当前浏览器不支持 WebSocket');
			return 0;
		}
	};
	
	// 定义停止与发送函数
	this.wsStop = function () {
		if(reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
		if(speechSokt != undefined) {
			console.log("stop ws!");
			speechSokt.close();
		}
	};
	
	this.wsSend = function ( oneData ) {
 
		if(speechSokt == undefined) return;
		if ( speechSokt.readyState === 1 ) { // 0:CONNECTING, 1:OPEN, 2:CLOSING, 3:CLOSED
 
			speechSokt.send( oneData );
 
			
		}
	};
	
	// 读取当前数字人连接的 sessionid（console 页面里的隐藏输入框）
	function getSessionId(){
		try{
			return String(parent.document.getElementById('sessionid').value || '');
		}catch(e){
			return '';
		}
	}

	// SOCEKT连接中的消息与状态响应
	function onOpen( e ) {
		// 发送json
		var chunk_size = new Array( 5, 10, 5 );
		var request = {
			"chunk_size": chunk_size,
			"wav_name":  "h5",
			"is_speaking":  true,
			"chunk_interval":10,
			"itn":getUseITN(),
			"mode":getAsrMode(),
			// 数字人连接号：后端据此把音频归到正确的对话会话
			"sessionid":getSessionId(),

		};
		if(isfilemode)
		{
			request.wav_format=file_ext;
			if(file_ext=="wav")
			{
				request.wav_format="PCM";
				request.audio_fs=file_sample_rate;
			}
		}
		
		var hotwords=getHotwords();
 
		if(hotwords!=null  )
		{
			request.hotwords=hotwords;
		}
		console.log(JSON.stringify(request));
		speechSokt.send(JSON.stringify(request));
		console.log("连接成功");
		stateHandle(0);
 
	}
	
	function onClose( e ) {
		stateHandle(1);
		scheduleReconnect();
	}
	
	function onMessage( e ) {
 
		msgHandle( e );
	}
	
	function onError( e ) {
 
		info_div.innerHTML="连接"+e;
		console.log(e);
		stateHandle(2);
		scheduleReconnect();
		
	}
	
	function scheduleReconnect() {
		if(reconnectTimer) return;
		reconnectTimer = setTimeout(function() {
			reconnectTimer = null;
			console.log("ws reconnect...");
			info_div.innerHTML="连接断开，自动重连中...";
			if(typeof manualStopped !== 'undefined') manualStopped = false;
			var ret = wsconnecter.wsStart();
			if(ret == 1) {
				console.log("ws reconnect success");
				// 重连成功后自动开始录音
				if(typeof isRec !== 'undefined' && isRec !== true) {
					rec.open(function(){
						rec.start();
						console.log("reconnect: auto restart record");
						if(typeof btnStart !== 'undefined') btnStart.disabled = true;
						if(typeof btnStop !== 'undefined') btnStop.disabled = false;
						if(typeof btnConnect !== 'undefined') btnConnect.disabled = true;
					});
				}
			} else {
				console.log("ws reconnect failed, retry later");
				scheduleReconnect();
			}
		}, 2000);
	}
    
 
}