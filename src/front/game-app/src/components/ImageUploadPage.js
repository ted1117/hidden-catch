import React, { useState } from 'react';
import './ImageUploadPage.css';

function ImageUploadPage({ onNavigate }) {
  const [uploadedImages, setUploadedImages] = useState([]);
  const [previews, setPreviews] = useState([]);
  const [isWaitingGame, setIsWaitingGame] = useState(false); // 게임 준비 대기 상태
  const MAX_IMAGES = 3;
  

  // 이미지 파일 검증
  const validateImageFile = (file) => {
    const allowedTypes = ['image/jpeg', 'image/jpg', 'image/png'];
    return allowedTypes.includes(file.type);
  };

  // 이미지 업로드 핸들러
  const handleImageUpload = (e) => {
    const files = Array.from(e.target.files);
    
    if (uploadedImages.length + files.length > MAX_IMAGES) {
      alert(`최대 ${MAX_IMAGES}장까지만 업로드 가능합니다.`);
      return;
    }

    const validFiles = files.filter(validateImageFile);
    
    if (validFiles.length !== files.length) {
      alert('jpg, jpeg, png 파일만 업로드 가능합니다.');
    }

    if (validFiles.length === 0) return;

    // 미리보기 생성
    const newPreviews = [];
    for (const file of validFiles) {
      const reader = new FileReader();
      reader.onload = (e) => {
        newPreviews.push(e.target.result);
        if (newPreviews.length === validFiles.length) {
          setPreviews([...previews, ...newPreviews]);
        }
      };
      reader.readAsDataURL(file);
    }

    // 업로드된 파일 목록에 추가
    setUploadedImages([...uploadedImages, ...validFiles]);
  };

  // 게임 시작 버튼 핸들러
  const handleStartGame = async () => {
    if (uploadedImages.length === 0) {
      alert('이미지를 먼저 업로드해주세요.');
      return;
    }

    try {
      setIsWaitingGame(true); // 대기 상태 시작
      
      // 1. 게임 생성 요청
      const gameResponse = await fetch('/api/v1/games', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          mode: 'single',
          difficulty: 'easy',
          time_limit_seconds: 180,
          requested_slot_count: uploadedImages.length
        }),
      });

      if (!gameResponse.ok) {
        alert('게임 생성 요청에 실패했습니다.');
        return;
      }

      const gameData = await gameResponse.json();
      const { game_id, upload_slots } = gameData;

      console.log('게임 생성 완료:', gameData);
      
      // game_id를 localStorage에 저장
      localStorage.setItem('currentGameRoomId', game_id.toString());

      // 2. 각 이미지를 S3에 업로드
      for (let i = 0; i < uploadedImages.length; i++) {
        const file = uploadedImages[i];
        const slot = upload_slots[i];

        // S3에 이미지 업로드
        const s3Response = await fetch(slot.presigned_url, {
          method: 'PUT',
          body: file,
          headers: {
            'Content-Type': file.type,
          },
        });

        if (!s3Response.ok) {
          alert(`이미지 ${i + 1} 업로드에 실패했습니다.`);
          return;
        }

        console.log(`S3 업로드 완료 (slot ${slot.slot})`);

        // 3. 업로드 완료 알림
        const completeResponse = await fetch(`/api/v1/games/${game_id}/uploads/complete`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            slot: slot.slot
          }),
        });

        if (!completeResponse.ok) {
          alert(`업로드 완료 알림 전송에 실패했습니다 (slot ${slot.slot}).`);
          return;
        }

        const completeData = await completeResponse.json();
        console.log('업로드 완료 알림 성공:', completeData);
      }

      // 4. 상태 폴링 (1초마다 확인)
      let pollTimeoutId = null;
      const pollStatus = async () => {
        const statusResponse = await fetch(`/api/v1/games/${game_id}`);
        
        if (!statusResponse.ok) {
          console.error('상태 조회 실패');
          return;
        }

        const statusData = await statusResponse.json();
        console.log('게임 상태:', statusData);

        if (statusData.status === 'playing') {
          // 게임 시작 가능 상태
          setIsWaitingGame(false);
          onNavigate('game');
        } else {
          // 아직 준비 중이면 1초 후 재시도
          pollTimeoutId = setTimeout(pollStatus, 1000);
        }
      };

      // 폴링 시작
      pollStatus();

    } catch (error) {
      console.error('게임 시작 에러:', error);
      alert('게임 시작 중 오류가 발생했습니다.');
      setIsWaitingGame(false); // 에러 시 대기 상태 해제
    }
  };

  // 뒤로가기 버튼 핸들러
  const handleGoBack = () => {
    setPreviews([]);
    setUploadedImages([]);
    onNavigate('home');
  };

  return (
    <div className="upload-page">
      {isWaitingGame && (
        <div className="waiting-overlay">
          <div className="waiting-content">
            <div className="spinner"></div>
            <h3>게임 준비 중...</h3>
            <p>AI가 퍼즐을 생성하고 있습니다. 잠시만 기다려주세요.</p>
          </div>
        </div>
      )}
      
      <div className="upload-content">
        <h2>이미지 업로드</h2>
        <p className="upload-info">최대 {MAX_IMAGES}장까지 업로드 가능 (jpg, jpeg, png)</p>
        
        <div 
          className={`upload-area ${uploadedImages.length >= MAX_IMAGES ? 'disabled' : ''}`}
          onClick={() => {
            if (uploadedImages.length < MAX_IMAGES) {
              document.getElementById('image-input')?.click();
            }
          }}
        >
          <input
            type="file"
            id="image-input"
            multiple
            accept=".jpg,.jpeg,.png"
            onChange={handleImageUpload}
            style={{ display: 'none' }}
            disabled={uploadedImages.length >= MAX_IMAGES}
          />
          
          {previews.length === 0 ? (
            <div className="upload-empty-state">
              <div className="upload-icon">
                <svg width="100" height="100" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                  <path d="M7 18C4.23858 18 2 15.7614 2 13C2 10.2386 4.23858 8 7 8C7.33962 8 7.67482 8.02857 8.00289 8.08296C8.00329 8.05531 8.00362 8.02759 8.00388 8C8.00388 5.23858 10.2425 3 13.0039 3C15.7653 3 18.0039 5.23858 18.0039 8C18.0039 8.02759 18.0042 8.05531 18.0046 8.08296C18.3327 8.02857 18.6679 8 19.0075 8C21.769 8 24.0075 10.2386 24.0075 13C24.0075 15.7614 21.769 18 19.0075 18" stroke="#b0b0b0" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                  <path d="M12 11V17M9 14H15" stroke="#b0b0b0" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
              </div>
              <p className="upload-instruction">이미지를 드래그하거나 클릭하여 업로드하세요</p>
              <div className={`upload-label ${uploadedImages.length >= MAX_IMAGES ? 'disabled' : ''}`}>
                {uploadedImages.length >= MAX_IMAGES 
                  ? '최대 업로드 개수 도달' 
                  : '+ 이미지 업로드'}
              </div>
            </div>
          ) : (
            <div className="preview-container">
              {previews.map((preview, index) => (
                <div key={index} className="preview-item">
                  <img src={preview} alt={`미리보기 ${index + 1}`} />
                  <span className="preview-number">{index + 1}</span>
                </div>
              ))}
              {uploadedImages.length < MAX_IMAGES && (
                <div 
                  className="upload-label-small"
                  onClick={(e) => {
                    e.stopPropagation();
                    document.getElementById('image-input')?.click();
                  }}
                >
                  + 추가 업로드
                </div>
              )}
            </div>
          )}
        </div>

        <div className="button-group">
          <button onClick={handleGoBack} className="back-button">
            뒤로가기
          </button>
          <button 
            onClick={handleStartGame} 
            className="start-button"
            disabled={uploadedImages.length === 0}
          >
            게임 시작
          </button>
        </div>
      </div>
    </div>
  );
}

export default ImageUploadPage;
