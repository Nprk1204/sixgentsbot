document.addEventListener('DOMContentLoaded', function() {
    const checkRankButton = document.getElementById('checkRankButton');
    const resultModal = new bootstrap.Modal(document.getElementById('resultModal'));
    
    checkRankButton.addEventListener('click', function() {
        const rocketId = document.getElementById('rocketId').value.trim();
        const platform = document.getElementById('platform').value;
        const discordId = document.getElementById('discordId').value.trim();
        
        if (!rocketId) {
            alert('Please enter your Rocket League ID');
            return;
        }
        
        // Show loading state
        document.getElementById('verificationResult').innerHTML = `
            <div class="text-center py-3">
                <div class="spinner-border text-primary" role="status">
                    <span class="visually-hidden">Loading...</span>
                </div>
                <p class="mt-2">Checking your rank via RLTracker...</p>
            </div>
        `;
        
        // Disable the button while checking
        checkRankButton.disabled = true;
        checkRankButton.innerHTML = `
            <span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>
            Checking...
        `;
        
        // Call our API endpoint to get the rank data
        fetchRankData(rocketId, platform, discordId);
    });
    
    function fetchRankData(username, platform, discordId) {
        // Build the API URL with query parameters
        let apiUrl = `/api/rank-check?platform=${encodeURIComponent(platform)}&username=${encodeURIComponent(username)}`;
        
        if (discordId) {
            apiUrl += `&discord_id=${encodeURIComponent(discordId)}`;
        }
        
        // Make the API request
        fetch(apiUrl)
            .then(response => response.json())
            .then(data => {
                // Process the API response
                if (data.success) {
                    displayRankResults(data, discordId);
                } else {
                    showError(data.message || "Could not retrieve rank information. Please check your ID and platform.");
                }
            })
            .catch(error => {
                console.error('Error:', error);
                showError("An error occurred while checking your rank. Please try again later.");
            })
            .finally(() => {
                // Re-enable button regardless of outcome
                checkRankButton.disabled = false;
                checkRankButton.innerHTML = `<i class="fas fa-check-circle me-2"></i> Check My Rank`;
            });
    }
    
    function displayRankResults(data, discordId) {
        // Update verification result section
        let resultHtml = `
            <div class="alert alert-success mb-4">
                <i class="fas fa-check-circle me-2"></i> Rank verification successful!
            </div>
            
            <div class="d-flex justify-content-between align-items-center mb-4">
                <div>
                    <h5 class="mb-1">${data.username}</h5>
                    <p class="mb-0 text-muted">${getPlatformName(data.platform)}</p>
                </div>
                <a href="${data.profileUrl}" target="_blank" class="btn btn-sm btn-outline-primary">
                    View RLTracker Profile
                </a>
            </div>
            
            <div class="d-flex align-items-center mb-3">
                <div class="me-3">
                    <i class="fas fa-trophy fa-2x text-warning"></i>
                </div>
                <div>
                    <h6 class="mb-0">Detected 3v3 Rank</h6>
                    <p class="mb-0">${data.rank}</p>
                </div>
            </div>
            
            <div class="d-flex align-items-center mb-3">
                <div class="me-3">
                    <i class="fas fa-tag fa-2x text-info"></i>
                </div>
                <div>
                    <h6 class="mb-0">Assigned Role</h6>
                    <p class="mb-0"><span class="badge bg-${getRoleColor(data.tier)}">${data.tier}</span></p>
                </div>
            </div>
            
            <div class="d-flex align-items-center">
                <div class="me-3">
                    <i class="fas fa-chart-line fa-2x text-success"></i>
                </div>
                <div>
                    <h6 class="mb-0">Starting MMR</h6>
                    <p class="mb-0">${data.mmr}</p>
                </div>
            </div>
        `;
        
        // Add Discord role assignment status if applicable
        if (discordId && data.role_assignment) {
            const roleSuccess = data.role_assignment.success;
            resultHtml += `
                <div class="alert alert-${roleSuccess ? 'success' : 'warning'} mt-3">
                    <i class="fab fa-discord me-2"></i> 
                    ${roleSuccess ? 'Discord role assigned successfully!' : 'Could not assign Discord role automatically. Please contact an admin.'}
                </div>
            `;
        }
        
        document.getElementById('verificationResult').innerHTML = resultHtml;
        
        // Update modal
        let modalContent = `
            <div class="text-center">
                <div class="mb-3">
                    <i class="fas fa-check-circle fa-4x text-success"></i>
                </div>
                <h4>Your rank has been verified!</h4>
                <p class="mb-3">Based on your ${data.rank} rank in competitive 3v3.</p>
                
                <div class="alert alert-info">
                    <strong>Discord Role:</strong> ${data.tier}<br>
                    <strong>Starting MMR:</strong> ${data.mmr}
                </div>
        `;
        
        if (discordId) {
            if (data.role_assignment && data.role_assignment.success) {
                modalContent += `
                    <div class="alert alert-success">
                        <i class="fab fa-discord me-2"></i> Your Discord role has been updated automatically!
                    </div>
                `;
            } else {
                modalContent += `
                    <div class="alert alert-warning">
                        <i class="fas fa-exclamation-triangle me-2"></i> Could not update your Discord role automatically. Please contact an admin.
                    </div>
                `;
            }
        } else {
            modalContent += `
                <div class="alert alert-warning">
                    <i class="fas fa-exclamation-triangle me-2"></i> No Discord ID provided. Please contact an admin to get your role assigned.
                </div>
            `;
        }
        
        modalContent += `
                <p>You can now join the 6 Mans queue in our Discord server.</p>
                <a href="https://discord.gg/your-discord" target="_blank" class="btn btn-primary mt-2">
                    <i class="fab fa-discord me-2"></i> Join Our Discord
                </a>
            </div>
        `;
        
        document.getElementById('resultModalContent').innerHTML = modalContent;
        
        // Show modal
        resultModal.show();
    }
    
    function showError(message) {
        // Update verification result section
        document.getElementById('verificationResult').innerHTML = `
            <div class="alert alert-danger mb-0">
                <i class="fas fa-exclamation-circle me-2"></i> ${message}
            </div>
        `;
        
        // Update modal
        document.getElementById('resultModalContent').innerHTML = `
            <div class="text-center">
                <div class="mb-3">
                    <i class="fas fa-exclamation-circle fa-4x text-danger"></i>
                </div>
                <h4>Verification Failed</h4>
                <div class="alert alert-danger mb-0">
                    ${message}
                </div>
                <p class="mt-3">Please check your Rocket League ID and platform, then try again.</p>
                <p class="mt-3">If the problem persists, you can still join our Discord and request manual verification from an admin.</p>
                <a href="https://discord.gg/your-discord" target="_blank" class="btn btn-primary mt-2">
                    <i class="fab fa-discord me-2"></i> Join Our Discord
                </a>
            </div>
        `;
        
        // Show modal
        resultModal.show();
    }
    
    function getPlatformName(platformCode) {
        const platforms = {
            'epic': 'Epic Games',
            'steam': 'Steam',
            'psn': 'PlayStation',
            'xbl': 'Xbox',
            'switch': 'Nintendo Switch'
        };
        
        return platforms[platformCode] || platformCode;
    }
    
    function getRoleColor(tier) {
        const colors = {
            'Champion': 'danger',
            'Diamond': 'primary',
            'Platinum': 'purple-rank',
            'Gold': 'warning'
        };
        
        return colors[tier] || 'secondary';
    }
});