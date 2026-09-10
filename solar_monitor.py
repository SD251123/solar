import os
import time
from datetime import datetime, timezone, timedelta
import requests
from tqdm import tqdm
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# 깃허브 시크릿(os.environ)에서 민감 정보를 안전하게 불러오기
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SOLAR_ID = os.environ.get("SOLAR_ID")
SOLAR_PW = os.environ.get("SOLAR_PW")

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            print("📲 텔레그램 알림 전송 완료!")
        else:
            print(f"❌ 텔레그램 전송 실패: {response.text}")
    except Exception as e:
        print(f"❌ 텔레그램 API 통신 에러: {e}")

def monitor_solar_status(user_id, user_pw):
    print("🚀 깃허브 서버 환경(헤드리스)에서 태양광 발전소 순찰 모니터링 중...")
    
    # 한국 시간(KST, UTC+9) 계산
    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(kst)
    current_hour = now_kst.hour
    current_minute = now_kst.minute
    current_time_str = now_kst.strftime('%Y-%m-%d %H:%M:%S')

    options = webdriver.ChromeOptions()
    # 서버 환경 구동을 위한 필수 헤드리스 및 보안 옵션 적용
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    
    try:
        driver.get("https://solar.mrt.co.kr/")
        wait = WebDriverWait(driver, 15)
        
        # 1. 로그인
        print("🔑 로그인을 진행하고 있습니다...")
        for _ in tqdm(range(2), desc="로그인 준비 중"):
            time.sleep(0.3)
            
        id_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text'], input[placeholder*='아이디']")))
        id_input.clear()
        id_input.send_keys(user_id)
        
        pw_input = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
        pw_input.clear()
        pw_input.send_keys(user_pw)
        
        driver.find_element(By.CSS_SELECTOR, "button[type='submit'], .login-btn, button").click()
        
        print("⏳ 대시보드 데이터 로딩 대기 중...")
        for _ in tqdm(range(6), desc="데이터 렌더링 대기 중"):
            time.sleep(1)
        
        total_issues = 0
        detected_source = ""
        
        # 2-1. '경보' 뱃지 체크
        for _ in tqdm(range(1), desc="경보 데이터 확인"):
            try:
                alarm_badge = driver.find_element(By.CSS_SELECTOR, "span.Header__alarm-count___13qAd")
                alarm_text = alarm_badge.text.strip()
                if alarm_text.isdigit():
                    val = int(alarm_text)
                    if val > 0:
                        total_issues += val
                        detected_source += f"경보 {val}건 "
            except Exception:
                pass 

        # 2-2. '오류' 뱃지 체크
        for _ in tqdm(range(1), desc="오류 데이터 확인"):
            try:
                error_div = driver.find_element(By.CSS_SELECTOR, "div.warning span")
                error_text = error_div.text.strip()
                
                if "오류(" in error_text:
                    start_idx = error_text.find('(') + 1
                    end_idx = error_text.find(')')
                    if start_idx > 0 and end_idx > start_idx:
                        num_str = error_text[start_idx:end_idx]
                        if num_str.isdigit():
                            val = int(num_str)
                            if val > 0:
                                total_issues += val
                                detected_source += f"오류 {val}건 "
            except Exception:
                pass 

        print(f"🔍 실시간 통합 진단 결과 -> 총 감지된 이상 징후: {total_issues}건 ({detected_source})")

        # 3. 알림 전송 종합 로직
        # 3-1. 이상이 감지된 경우 즉시 긴급 경고 발송
        if total_issues > 0:
            alert_msg = (
                "🚨 *[태양광 발전소 긴급 이상 경고]*\n\n"
                f"• 감지 상태: *{detected_source.strip()}* 발생!\n"
                "• 매곡 등 이상이 생긴 발전소를 즉시 확인해 주세요.\n\n"
                f"• 확인 시간: {current_time_str}"
            )
            send_telegram_message(alert_msg)
            print("🚨 문제가 감지되어 텔레그램으로 즉시 긴급 알림을 전송했습니다.")
        
        # 3-2. 정기 리포트 시간(12시, 15시, 18시)이면서, 정각 크론 타임(예: 30분 미만에 실행된 경우)에만 정상 가동 리포트 발송
        if current_hour in [12, 15, 18] and current_minute < 30:
            heartbeat_msg = (
                "🟢 *[태양광 봇 정기 가동 리포트]*\n\n"
                "• 상태: 정상 가동 중 🛡️\n"
                "• 현재 시간대 순찰 점검이 완료되었습니다.\n\n"
                f"• 확인 시간: {current_time_str}"
            )
            send_telegram_message(heartbeat_msg)
            print(f"🟢 정기 리포트 시간대({current_hour}시 정각 턴) 도래: 텔레그램으로 정상 가동 메시지를 전송했습니다.")
        else:
            print("🟢 정기 리포트 조건에 해당하지 않음 (30분 단위 순찰 또는 시간대 불일치).")
        
        if total_issues == 0 and not (current_hour in [12, 15, 18] and current_minute < 30):
            print("🟢 이상 없음. 추가 알림은 생략합니다.")

    except Exception as e:
        error_msg = f"❌ *[모니터링 스크립트 실행 오류]*\n`{str(e)}`"
        send_telegram_message(error_msg)
        print(error_msg)
    
    finally:
        for _ in tqdm(range(2), desc="브라우저 세션 정리 중"):
            time.sleep(0.5)
        driver.quit()
        print("🔒 깃허브 서버 세션이 안전하게 종료되었습니다.")

if __name__ == "__main__":
    if not SOLAR_ID or not SOLAR_PW or not TELEGRAM_TOKEN:
        print("❌ 오류: 깃허브 시크릿 환경변수가 설정되지 않았습니다. (SOLAR_ID, SOLAR_PW, TELEGRAM_TOKEN 확인 필요)")
    else:
        monitor_solar_status(SOLAR_ID, SOLAR_PW)
