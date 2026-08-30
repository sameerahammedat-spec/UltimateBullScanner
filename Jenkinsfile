pipeline {
    agent { label 'windows-python' }

    options {
        timestamps()
        disableConcurrentBuilds()
        timeout(time: 5, unit: 'HOURS')
        skipDefaultCheckout(false)
    }

    triggers {
        // Jenkins controller/agent timezone MUST be Asia/Kolkata.
        // Schedule: 13:30 IST, Monday-Friday.
        cron('30 13 * * 1-5')
    }

    environment {
        PYTHONUNBUFFERED = '1'
        TZ = 'Asia/Kolkata'
        NTFY_TOPIC = credentials('NTFY_TOPIC')
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Prepare Python environment') {
            steps {
                bat '''
                if not exist .venv python -m venv .venv
                .venv\\Scripts\\python.exe -m pip install --upgrade pip
                .venv\\Scripts\\python.exe -m pip install -r Scanner\\requirements.txt
                .venv\\Scripts\\python.exe -m compileall -q Scanner
                '''
            }
        }

        stage('Run live scanners') {
            steps {
                powershell '''
                    $python = Join-Path $env:WORKSPACE '.venv\\Scripts\\python.exe'
                    $scanner = Join-Path $env:WORKSPACE 'Scanner'
                    $out = Join-Path $env:WORKSPACE 'Scanner\\jenkins_logs'
                    New-Item -ItemType Directory -Force -Path $out | Out-Null

                    $afternoonLog = Join-Path $out 'afternoon.log'
                    $resultsLog = Join-Path $out 'results_pre_event.log'

                    Write-Host "Starting afternoon scanner and results pre-event scanner at $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz'))"

                    $p1 = Start-Process -FilePath $python -ArgumentList @(
                        'afternoon_fresh_move_scanner.py', '--watch',
                        '--group-a','Group_A.csv', '--group-b','Group_B.csv',
                        '--smallcap','bse_smallcap250_clean.csv', '--output-dir','afternoon_reports'
                    ) -WorkingDirectory $scanner -RedirectStandardOutput $afternoonLog -RedirectStandardError ($afternoonLog + '.err') -PassThru -WindowStyle Hidden

                    $p2 = Start-Process -FilePath $python -ArgumentList @(
                        'results_pre_event_scanner.py', '--watch', '--lookahead-days','3',
                        '--output-dir','afternoon_reports'
                    ) -WorkingDirectory $scanner -RedirectStandardOutput $resultsLog -RedirectStandardError ($resultsLog + '.err') -PassThru -WindowStyle Hidden

                    Wait-Process -Id $p1.Id, $p2.Id
                    $p1.Refresh(); $p2.Refresh()

                    Write-Host "Afternoon scanner exit code: $($p1.ExitCode)"
                    Write-Host "Results pre-event scanner exit code: $($p2.ExitCode)"

                    Get-Content $afternoonLog -Tail 200
                    Get-Content $resultsLog -Tail 200

                    if ($p1.ExitCode -ne 0 -or $p2.ExitCode -ne 0) {
                        throw "One or more scanners failed. Afternoon=$($p1.ExitCode), Results=$($p2.ExitCode)"
                    }
                '''
            }
        }
    }

    post {
        always {
            archiveArtifacts artifacts: 'Scanner/afternoon_reports/**/*,Scanner/jenkins_logs/**/*', allowEmptyArchive: true, fingerprint: true
        }
    }
}
