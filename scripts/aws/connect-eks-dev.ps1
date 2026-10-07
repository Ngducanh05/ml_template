$ErrorActionPreference = "Stop"

$Profile = "ml-template-dev"
$Region  = "ap-southeast-1"
$Cluster = "ml-template-dev-sg"
$Context = "ml-template-dev-sg"

Write-Host "=== EKS Singapore Connect ==="

# 1. Verify AWS identity
Write-Host "`n[1/5] AWS identity"
aws sts get-caller-identity `
  --profile $Profile `
  --region $Region

if ($LASTEXITCODE -ne 0) {
    throw "AWS identity check failed"
}

# 2. Detect current public IP
Write-Host "`n[2/5] Detect current public IP"
$IP = (Invoke-RestMethod "https://checkip.amazonaws.com").Trim()
$CIDR = "$IP/32"

Write-Host "Current CIDR: $CIDR"

# 3. Read current EKS endpoint whitelist
Write-Host "`n[3/5] Check EKS endpoint CIDR"
$ClusterInfo = aws eks describe-cluster `
  --name $Cluster `
  --region $Region `
  --profile $Profile | ConvertFrom-Json

$CurrentCidrs = @($ClusterInfo.cluster.resourcesVpcConfig.publicAccessCidrs)

if ($CIDR -notin $CurrentCidrs) {
    Write-Host "IP changed. Updating EKS publicAccessCidrs..."

    $Update = aws eks update-cluster-config `
      --name $Cluster `
      --resources-vpc-config "endpointPublicAccess=true,publicAccessCidrs=$CIDR" `
      --region $Region `
      --profile $Profile | ConvertFrom-Json

    $UpdateId = $Update.update.id

    do {
        Start-Sleep -Seconds 5

        $Status = aws eks describe-update `
          --name $Cluster `
          --update-id $UpdateId `
          --region $Region `
          --profile $Profile `
          --query "update.status" `
          --output text

        Write-Host "CIDR update: $Status"

        if ($Status -eq "Failed" -or $Status -eq "Cancelled") {
            throw "EKS CIDR update failed: $Status"
        }

    } while ($Status -ne "Successful")
}
else {
    Write-Host "Current IP already allowed."
}

# 4. Refresh kubeconfig
Write-Host "`n[4/5] Update kubeconfig"

aws eks update-kubeconfig `
  --name $Cluster `
  --region $Region `
  --profile $Profile `
  --alias $Context

if ($LASTEXITCODE -ne 0) {
    throw "Failed to update kubeconfig"
}

# 5. Verify cluster access
Write-Host "`n[5/5] Verify nodes"

kubectl get nodes --context $Context

if ($LASTEXITCODE -ne 0) {
    throw "kubectl cannot reach EKS cluster"
}

Write-Host "`nEKS Singapore connection OK."