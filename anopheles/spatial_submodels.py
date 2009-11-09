import numpy as np
import cov_prior
from mahalanobis_covariance import *
import pymc as pm

__all__ = ['spatial_hill','hill_fn','hinge','step','lr_spatial','lr_spatial_env','MVNLRParentMetropolis','minimal_jumps','bookend','spatial_env','nogp_spatial_env','normalize_env']

def hinge(x, cp):
    "A MaxEnt hinge feature"
    y = x-cp
    y[x<cp]=0.
    return y
    
def step(x,cp):
    "A MaxEnt step feature"
    y = np.ones(x.shape)
    y[x<cp]=0.
    return y


# =============================
# = The standard spatial hill =
# =============================
class hill_fn(object):
    "Closure used by spatial_hill"    
    def __init__(self, val, vec, ctr, amp):
        self.val = val
        self.vec = vec
        self.ctr = ctr
        self.amp = amp
        self.max_argsize = np.inf
        
    def __call__(self, x):
        xr = x.reshape(-1,2)
        dev = xr-self.ctr
        tdev = np.dot(dev, self.vec)
        if len(dev.shape)==1:
            ax=0
        else:
            ax=-1
        return np.sum(tdev**2/self.val,axis=ax)*self.amp


def spatial_hill(**kerap):
    "For debugging only"
    amp = pm.Uninformative('amp',-1)
    
    @pm.stochastic
    def ctr(value=np.array([0,0])):
        "This makes the center uniformly distributed over the surface of the earth."
        if value[0] < -np.pi or value[0] > np.pi or value[1] < -np.pi/2. or value[1] > np.pi/2.:
            return -np.inf
        return np.cos(value[1])

    bump_eigenvalues = pm.Gamma('bump_eigenvalues', 2, 2, size=2)
    bump_eigenvectors = cov_prior.OrthogonalBasis('bump_eigenvectors',2)
    
    @pm.deterministic
    def p(val = bump_eigenvalues, vec = bump_eigenvectors, ctr = ctr, amp=amp):
        "A stupid hill, using Euclidean distance."
        return hill_fn(val, vec, ctr, amp)
        
    return locals()


# =======================================
# = The spatial-only, low-rank submodel =
# =======================================
def mod_matern(x,y,diff_degree,amp,scale,symm=False):
    """Matern with the mean integrated out."""
    return pm.gp.matern.geo_rad(x,y,diff_degree=diff_degree,amp=amp,scale=scale,symm=symm)+10000

def bookend(A, Ufro, Ubak):
    return np.dot(pm.gp.trisolve(Ufro[:,:Ufro.shape[0]], A.T, uplo='U', transa='N'), Ubak[:,:Ubak.shape[0]]).T

def minimal_jumps(piv_old, U_old, piv_new, U_new):
    """
    Returns the matrices giving the minimal-squared-error forward and backward jumps
    when piv_new and U_new are proposed as replacements for piv_old and U_old.
    
    Converts first to the independent unit normals underlying the current and
    proposed states.
    """
    U_old_sorted = U_old[:,np.argsort(piv_old)]
    U_new_sorted = U_new[:,np.argsort(piv_new)]
    
    oldnew = np.dot(U_old_sorted, U_new_sorted.T)
    oldold = np.dot(U_old_sorted, U_old_sorted.T)
    newnew = np.dot(U_new_sorted, U_new_sorted.T)
    
    forjump = np.linalg.solve(newnew, oldnew.T)
    bakjump = np.linalg.inv(np.linalg.solve(oldold, oldnew))
    
    return bookend(forjump, U_old, U_new), bookend(bakjump, U_old, U_new)
        
class MVNLRParentMetropolis(pm.AdaptiveMetropolis):
    def __init__(self, variables, mvn, U, piv, rl, cov=None, delay=1000, scales=None, interval=200, greedy=True, shrink_if_necessary=False, verbose=0, tally=False):
        pm.AdaptiveMetropolis.__init__(self, variables, cov, delay, scales, interval, greedy, shrink_if_necessary,verbose, tally)
        self.mvn = mvn
        self.piv = piv
        self.U = U
        self.rl = rl
        for s in self.markov_blanket:
            if s.__name__ == 'data':
                self.data = s
                break
        
    def propose(self):
        piv_old = self.piv.value
        U_old = self.U.value
        
        self.cur_mvn_val = self.mvn.value
        self.mvn_jumped = False
        
        pm.AdaptiveMetropolis.propose(self)
        try:
            self.logp_plus_loglike
        except pm.ZeroProbability:
            self.reject()
        forjump, bakjump = minimal_jumps(piv_old, U_old, self.piv.value, self.U.value)
        
        # Symmetric proposal
        if np.random.randint(2)==0:
            jump = forjump
        else:
            jump = bakjump
        
        self.mvn.value = np.dot(jump, self.mvn.value)
        self.mvn_jumped = True
        
    def reject(self):
        pm.AdaptiveMetropolis.reject(self)
        if self.mvn_jumped:
            if np.any(self.mvn.last_value != self.cur_mvn_val):
                raise RuntimeError, 'Rejection is going to fail'
            self.mvn.revert()
        
    def _get_logp_plus_loglike(self):
        sum = pm.utils.logp_of_set(self.markov_blanket + [self.mvn])
        if self.verbose>1:
            print '\t' + self._id + ' Current log-likelihood plus current log-probability', sum
        return sum
        
    logp_plus_loglike = property(_get_logp_plus_loglike)
        
        
class LRP(object):
    """A closure that can evaluate a low-rank field."""
    def __init__(self, x_fr, C, krige_wt, f2p):
        self.x_fr = x_fr
        self.C = C
        self.krige_wt = krige_wt
        self.f2p = f2p
    def __call__(self, x, f2p=None, offdiag=None):
        if f2p is None:
            f2p = self.f2p
        if offdiag is None:
            offdiag = self.C(x,self.x_fr)
        return f2p(np.dot(np.asarray(offdiag), self.krige_wt).reshape(x.shape[:-1]))
        
def lr_spatial(rl=50,**stuff):
    """A low-rank spatial-only model."""
    amp = pm.Exponential('amp',.1,value=1)
    scale = pm.Exponential('scale',.1,value=1.)
    diff_degree = pm.Uniform('diff_degree',0,2,value=.5)

    pts_in = stuff['pts_in']
    pts_out = stuff['pts_out']
    x_eo = np.vstack((pts_in, pts_out))

    @pm.deterministic
    def C(amp=amp,scale=scale,diff_degree=diff_degree):
        return pm.gp.Covariance(mod_matern, amp=amp, scale=scale, diff_degree=diff_degree)

    @pm.deterministic(trace=False)
    def ichol(C=C, rl=rl, x=x_eo):
        return C.cholesky(x, rank_limit=rl, apply_pivot=False)

    piv = pm.Lambda('piv', lambda d=ichol: d['pivots'])
    U = pm.Lambda('U', lambda d=ichol: d['U'].view(np.ndarray), trace=False)

    # Trace the full-rank locations
    x_fr = pm.Lambda('x_fr', lambda d=ichol, rl=rl, x=x_eo: x[d['pivots'][:rl]])

    # Evaluation of field at expert-opinion points
    U_fr = U[:rl,:rl]
    L_fr = pm.Lambda('L_fr', lambda U=U_fr: U.T)
    f_fr = pm.MvNormalChol('f_fr', np.zeros(rl), L_fr)   

    @pm.deterministic(trace=False)
    def krige_wt(f_fr = f_fr, U_fr = U_fr):
        return pm.gp.trisolve(U_fr,pm.gp.trisolve(U_fr,f_fr,uplo='U',transa='T'),uplo='U',transa='N',inplace=True)

    p = pm.Lambda('p', lambda x_fr=x_fr, C=C, krige_wt=krige_wt: LRP(x_fr, C, krige_wt))

    return locals()            
    
# ======================================
# = Spatial and environmental low-rank =
# ======================================
def mod_spatial_mahalanobis(x,y,val,vec,const_frac,symm=False):
    return spatial_mahalanobis_covariance(x,y,1,val,vec,const_frac,symm)

def normalize_env(x, means, stds):
    x_norm = x.copy().reshape(-1,x.shape[-1])
    for i in xrange(2,x_norm.shape[1]):
        x_norm[:,i] -= means[i-2]
        x_norm[:,i] /= stds[i-2]
    return x_norm

class LRP_norm(LRP):
    """
    A closure that can evaluate a low-rank field.
    
    Normalizes the third argument onward.
    """
    def __init__(self, x_fr, C, krige_wt, means, stds, f2p):
        LRP.__init__(self, x_fr, C, krige_wt, f2p)
        self.means = means
        self.stds = stds

    def __call__(self, x,f2p=None,offdiag=None):
        x_norm = normalize_env(x, self.means, self.stds)
        return LRP.__call__(self, x_norm.reshape(x.shape), f2p,offdiag)

def shapecheck_mv_normal_chol_like(x,mu,sig):
    """blah"""
    if sig.shape[1] != sig.shape[0]:
        return -np.inf
    else:
        return pm.mv_normal_chol_like(x,mu,sig)
        
ShapecheckMvNormalChol = pm.stochastic_from_dist('shapecheck_mv_normal_chol', shapecheck_mv_normal_chol_like, pm.rmv_normal_chol, mv=True)

class fullcond_fr_sampler(object):
    def __init__(self, x_fr, x_eo, n_in, n_out, C, vals_in, vals_out, nugget):
        self.x_fr = x_fr
        self.x_eo = x_eo
        self.n_in = n_in
        self.n_out = n_out
        self.C = C
        self.nugget = nugget
        
        # from IPython.Debugger import Pdb
        # Pdb(color_scheme='Linux').set_trace()   
        self.U_eo = self.C.cholesky(self.x_eo, nugget=self.nugget*np.ones(self.n_in+self.n_out))
        offdiag = self.C(self.x_eo, self.x_fr)
        self.o_U_eo = pm.gp.trisolve(self.U_eo,offdiag,uplo='U',transa='T')
        C_cond = self.C(self.x_fr, self.x_fr)-np.dot(self.o_U_eo.T,self.o_U_eo)
        self.L_cond = np.linalg.cholesky(C_cond)
        self.obs_val = np.concatenate((vals_in * np.ones(self.n_in), vals_out*np.ones(self.n_out)))
        self.M_cond = np.dot(self.o_U_eo.T, pm.gp.trisolve(self.U_eo, self.obs_val, transa='T', uplo='U'))
        
    def __call__(self):
        return np.asarray(self.M_cond + np.dot(self.L_cond, np.random.normal(size=self.x_fr.shape[0]))).ravel()
        

def lr_spatial_env(rl=200,**stuff):
    """A low-rank spatial-only model."""

    n_env = stuff['env_in'].shape[1]
    x_fr = normalize_env(stuff['full_x_fr'], stuff['env_means'], stuff['env_stds'])
    pts_in = stuff['pts_in']
    f2p = stuff['f2p']
    
    valpow = pm.Uniform('valpow',0,10,value=.01, observed=False)
    valmean = pm.Lambda('valmean',lambda valpow=valpow : np.arange(n_env+1)*valpow)
    valV = pm.Exponential('valV',1,value=.1)

    # val = pm.Normal('val',valmean,1./valV,value=np.concatenate(([-2],-1*np.ones(n_env))))
    vals = [pm.Normal('val_%i'%i,valmean[i],1./valV,value=0) for i in xrange(n_env+1)]
    vals[0].value = -1
    val = pm.Lambda('val',lambda vals=vals: np.array(vals))
    baseval = pm.Exponential('baseval', .01, value=np.exp(-1), observed=False)
    expval = pm.Lambda('expval',lambda val=val,baseval=baseval:np.exp(val)*baseval)    

    vec = cov_prior.OrthogonalBasis('vec',n_env+1,constrain=True)

    const_frac = pm.Uniform('const_frac',0,1,value=.1)
    
    @pm.deterministic
    def C(val=expval,vec=vec,const_frac=const_frac):
        return pm.gp.FullRankCovariance(mod_spatial_mahalanobis, val=val, vec=vec, const_frac=const_frac)
    
    # TODO tomorrow: Gibbs sample through to initialize to a good, constraint-satisfying state.   
    # Forget that, just Gibbs sample with constraint satisfaction! 
    @pm.deterministic
    def fullcond_sampler(C=C, vals_in=.25, vals_out=-.25, nugget=.01):
        try:
            return fullcond_fr_sampler(x_fr, normalize_env(stuff['full_x_eo'], stuff['env_means'], stuff['env_stds']),stuff['n_in'],stuff['n_out'],C,vals_in,vals_out,nugget)
        except np.linalg.LinAlgError:
            return None
    

    @pm.deterministic(trace=False)
    def U_fr(C=C, x=x_fr):
        try:
            return C.cholesky(x)
        except np.linalg.LinAlgError:
            return None

    @pm.potential
    def rank_check(U=U_fr):
        if U is None:
            return -np.inf
        else:
            return 0.

    L_fr = pm.Lambda('L',lambda U=U_fr: U.T, trace=False)

    # Evaluation of field at expert-opinion points
    init_val = np.ones(len(x_fr))*-.1
    # init_val[len(init_val)/2]=-1
    f_fr = pm.MvNormalChol('f_fr', np.zeros(len(x_fr)), L_fr, value=init_val)

    @pm.deterministic(trace=False)
    def krige_wt(f_fr=f_fr, U_fr=U_fr):
        return pm.gp.trisolve(U_fr,pm.gp.trisolve(U_fr,f_fr,uplo='U',transa='T'),uplo='U',transa='N',inplace=True)

    p = pm.Lambda('p', lambda x_fr=x_fr, C=C, krige_wt=krige_wt, means=stuff['env_means'], stds=stuff['env_stds'], f2p=f2p: LRP_norm(x_fr, C, krige_wt, means, stds, f2p))

    return locals()
    
def spatial_env(**stuff):
    """A spatial-only model."""
    # amp = pm.Exponential('amp',.1,value=10)
    const_frac = pm.Uniform('const_frac',0,1,value=.1)

    pts_in = np.hstack((stuff['pts_in'],stuff['env_in']))
    pts_out = np.hstack((stuff['pts_out'],stuff['env_out']))
    x_eo = normalize_env(np.vstack((pts_in, pts_out)), stuff['env_means'], stuff['env_stds'])
    x_fr = x_eo
    rl = x_eo.shape[0]

    n_env = stuff['env_in'].shape[1]

    # val = pm.Gamma('val',4,4,value=np.ones(n_env+1))
    val = pm.Exponential('val',.01,value=np.ones(n_env+1))
    vec = cov_prior.OrthogonalBasis('vec',n_env+1,constrain=True)

    @pm.deterministic
    def C(val=val,vec=vec,const_frac=const_frac):
        return pm.gp.FullRankCovariance(mod_spatial_mahalanobis, val=val, vec=vec, const_frac=const_frac)

    @pm.deterministic(trace=False)
    def U_fr(C=C, x=x_eo):
        try:
            return C.cholesky(x)
        except np.linalg.LinAlgError:
            return None

    @pm.potential
    def rank_check(U=U_fr):
        if U is None:
            return -np.inf
        else:
            return 0.

    L_fr = pm.Lambda('L',lambda U=U_fr: U.T)

    # Evaluation of field at expert-opinion points
    f_fr = pm.MvNormalChol('f_fr', np.zeros(rl), L_fr, value=np.ones(rl)*2)

    @pm.deterministic(trace=False)
    def krige_wt(f_fr=f_fr, U_fr=U_fr):
        if U_fr.shape == f_fr.shape*2:
            return pm.gp.trisolve(U_fr,pm.gp.trisolve(U_fr,f_fr,uplo='U',transa='T'),uplo='U',transa='N',inplace=True)
        else:
            return None

    p = pm.Lambda('p', lambda x_fr=x_eo, C=C, krige_wt=krige_wt, means=stuff['env_means'], stds=stuff['env_stds']: LRP_norm(x_fr, C, krige_wt, means, stds))

    return locals()

class RotatedLinearWithHill(object):
    """A closure used by nongp_spatial_env"""
    def __init__(self,val,vec,ctr,norm_means,norm_stds,hillpower,f2p):

        self.val = val
        self.vec = vec
        self.ctr = ctr
        self.norm_means = norm_means
        self.norm_stds = norm_stds
        self.hillpower=hillpower
        self.f2p = f2p

    def __call__(self, x, f2p=None, offdiag=None):
        if f2p is None:
            f2p = self.f2p
        x_ = normalize_env(x, self.norm_means, self.norm_stds)
        x__ = np.dot((x_.reshape(-1,x.shape[-1])[:,2:]-self.ctr),self.vec)
        
        quadpart = -np.sum((x__**2)*self.val,axis=1)**self.hillpower
        
        out = quadpart.reshape(x.shape[:-1])

        if np.any(np.isnan(out)):
            raise ValueError  
        return f2p(out)
    
def nogp_spatial_env(**stuff):
    """A low-rank spatial-only model."""

    n_env = stuff['env_in'].shape[1]

    # ctr = pm.Normal('ctr',np.zeros(n_env), np.ones(n_env), value=np.zeros(n_env),observed=False)
    # ctr = np.zeros(n_env)
    ctrs = [pm.Normal('ctr_%i'%i,0,1,value=0) for i in xrange(n_env)]
    ctr = pm.Lambda('ctr',lambda c=ctrs: np.array(c))
    C = lambda x1, x2: np.ones(2e5)
    
    # Encourage simplicity
    baseval = pm.Exponential('baseval', .001, value=1, observed=False)
    valpow = pm.Uniform('valpow',-10,0,value=-.01, observed=False)
    # valbeta = pm.Lambda('valbeta',lambda baseval=baseval,valpow=valpow:baseval*np.arange(1,n_env+1)**valpow)
    valV = pm.Lambda('valbeta',lambda baseval=baseval,valpow=valpow:baseval*np.arange(1,n_env+1)**valpow)
    val = pm.Normal('val',np.zeros(n_env),1./valV,value=np.ones(n_env))
    # vals = [pm.Normal('val_%i'%i,0,1./valV[i],value=0) for i in xrange(n_env)]
    # val = pm.Lambda('val',lambda v=vals: np.array(v).ravel())
    # val = pm.Exponential('val',valbeta,value=np.ones(n_env))

    vec = cov_prior.OrthogonalBasis('vec',(n_env),observed=False)
    # vec = np.eye(n_env)
    # hillpower = pm.Exponential('hillpower',.001,value=1,observed=True)
    hillpower = 1
    L_fr = np.eye(stuff['n_in']+stuff['n_out'])
    f_fr = pm.Normal('f_fr', 0, 1, size=stuff['n_in']+stuff['n_out'])
    
        
    p = pm.Lambda('p', lambda bv = val, be=vec, ctr=ctr, hillpower=hillpower: \
                            RotatedLinearWithHill(bv,be,ctr,stuff['env_means'],stuff['env_stds'],hillpower,stuff['f2p']))

    return locals()